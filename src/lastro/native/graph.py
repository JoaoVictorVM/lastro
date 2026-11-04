"""Grafo LangGraph de oito nós: preparar, memória, observar, decidir, agir, verificar, registrar e
encerrar. Os nós de decisão chamam o SDK Anthropic diretamente, pelo gateway com orçamento.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

from lastro.approvals.gate import AWAITING
from lastro.config.native import NativeSettings
from lastro.history import (
    ApprovalStatus,
    BudgetError,
    BudgetExceededError,
    EventType,
    HistoryStore,
    HistoryUnavailableError,
)
from lastro.native.budget import ACCOUNTING_VIOLATION, BudgetAccountingViolation
from lastro.native.human import HumanDecisionPort
from lastro.native.mcp import McpSessionAdapter, McpTransportError, ToolOutcome
from lastro.native.models import (
    EFFECT_TOOLS,
    MAX_INVALID_RESPONSES,
    MEMORY_TOOLS,
    PREPARATION_TOOLS,
    RECORD_TOOLS,
    REFERENCE_KEYS,
    NativeState,
    TerminalSignal,
)
from lastro.native.prompts import guidance, tool_data
from lastro.native.provider import (
    ApiAuthError,
    ApiUnavailableError,
    ContextLimitError,
    ModelGateway,
    ModelRequest,
    ModelResponse,
)

type Writer = Callable[[str], None]
MAX_RENEWED_GATES = 3


def response_problem(response: ModelResponse, seen_ids: set[str]) -> str | None:
    """Resposta estruturalmente inválida nunca dispara efeito, nem parcialmente."""
    if response.stop_reason == "max_tokens":
        return "incomplete_response"
    if response.stop_reason == "refusal":
        return "refusal"
    ids: set[str] = set()
    for block in response.tool_uses():
        tool_id = block.get("id")
        if not isinstance(tool_id, str) or not tool_id:
            return "missing_tool_use_id"
        if tool_id in ids or tool_id in seen_ids:
            return "duplicate_tool_use_id"
        ids.add(tool_id)
        if not isinstance(block.get("input"), dict):
            return "invalid_tool_input"
    return None


def tool_result(tool_id: str, content: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": tool_id, "content": content, "is_error": is_error}


def not_executed(tool_id: str, reason: str) -> dict[str, Any]:
    return tool_result(
        tool_id, json.dumps({"decision": "not_executed", "reason": reason}), is_error=True
    )


def summarize(name: str, outcome: ToolOutcome) -> str:
    data = outcome.data or {}
    for key in ("error", "decision", "status", "reason"):
        if key in data and isinstance(data[key], str | int | bool):
            return f"{name}: {key}={str(data[key])[:120]}"
    return f"{name}: ok"


@dataclass
class NativeAgent:
    history: HistoryStore
    run_id: str
    adapter: McpSessionAdapter
    gateway: ModelGateway
    human: HumanDecisionPort
    settings: NativeSettings
    system: list[dict[str, Any]]
    write: Writer
    tools: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    # Falhas de agente: auditadas como agent_failure, nunca como finding de produto.
    def agent_failure(self, code: str, detail: str, tool_name: str | None = None) -> None:
        payload: dict[str, Any] = {"code": code, "detail": detail[:500]}
        if tool_name is not None:
            payload["tool_name"] = tool_name[:100]
        try:
            self.history.append_event(self.run_id, EventType.AGENT_FAILURE, payload=payload)
        except HistoryUnavailableError:
            raise TerminalSignal("interrupted", "history_unavailable") from None
        self.write(f"! agent failure: {code}")

    def run_terminal(self) -> tuple[str, str] | None:
        record = self.history.get_run(self.run_id)
        if record.status.is_terminal:
            return record.status.value, record.status_reason or ""
        return None

    async def think(
        self, node: str, state: NativeState, allowed: frozenset[str] | None
    ) -> tuple[NativeState, list[dict[str, Any]]]:
        """Uma chamada de modelo; ids e schemas são validados antes de qualquer despacho."""
        messages = list(state.get("messages", []))
        content = [*state.get("pending_user", []), guidance(node)]
        messages.append({"role": "user", "content": content})
        step = state.get("step", 0) + 1
        request = ModelRequest(
            model=self.settings.model_for(node),
            system=self.system,
            messages=list(messages),
            tools=self.tools,
            max_tokens=self.settings.max_output_tokens,
        )
        try:
            response = await self.gateway.call(
                node, step, request, context_limit=self.settings.context_token_limit
            )
        except BudgetExceededError:
            raise TerminalSignal("limit_reached", "cost_cap") from None
        except BudgetAccountingViolation:
            raise TerminalSignal("limit_reached", ACCOUNTING_VIOLATION) from None
        except BudgetError as error:
            if error.code == ACCOUNTING_VIOLATION:
                raise TerminalSignal("limit_reached", ACCOUNTING_VIOLATION) from None
            raise TerminalSignal("interrupted", "history_unavailable") from None
        except ApiAuthError:
            self.write("ANTHROPIC_API_KEY is missing or invalid; the native executor cannot start.")
            raise TerminalSignal("blocked", "api_auth") from None
        except ApiUnavailableError:
            raise TerminalSignal("interrupted", "api_error") from None
        except ContextLimitError:
            raise TerminalSignal("interrupted", "context_limit") from None
        except HistoryUnavailableError:
            raise TerminalSignal("interrupted", "history_unavailable") from None
        messages.append({"role": "assistant", "content": response.content})
        usage = response.usage
        self.write(
            f"model {node} #{step}: "
            + (
                f"in {usage.total_input_tokens} out {usage.output_tokens} tokens"
                if usage is not None
                else "usage unknown"
            )
        )
        seen = set(state.get("seen_tool_ids", []))
        uses = response.tool_uses()
        problem = response_problem(response, seen)
        rejected: list[dict[str, Any]] = []
        if problem is None:
            for block in uses:
                reason = self.adapter.rejection(str(block.get("name")), block.get("input"))
                if reason is None and allowed is not None and block.get("name") not in allowed:
                    reason = f"{block.get('name')} is not available before this phase completes."
                if reason is not None:
                    self.agent_failure("invalid_tool_call", reason, str(block.get("name")))
                    rejected.append(block)
        else:
            self.agent_failure(problem, f"stop_reason={response.stop_reason}")
        streak = state.get("invalid_streak", 0)
        valid_ids = [str(block.get("id")) for block in uses if isinstance(block.get("id"), str)]
        updated: NativeState = {
            **state,
            "messages": messages,
            "pending_user": [],
            "step": step,
            "seen_tool_ids": [*seen, *valid_ids],
        }
        if problem is not None or rejected:
            updated["invalid_streak"] = streak + 1
            if streak + 1 >= MAX_INVALID_RESPONSES:
                raise TerminalSignal("interrupted", "agent_protocol_error")
            pending: list[dict[str, Any]] = []
            for block in uses:
                tool_id = str(block.get("id", ""))
                if block in rejected:
                    reason = self.adapter.rejection(str(block.get("name")), block.get("input"))
                    pending.append(
                        tool_result(
                            tool_id, reason or "Not available in this phase.", is_error=True
                        )
                    )
                else:
                    pending.append(not_executed(tool_id, problem or "batch_rejected"))
            if problem is not None:
                pending.append(
                    {"type": "text", "text": f"Your last response was invalid ({problem})."}
                )
            updated["pending_user"] = pending
            return updated, []
        updated["invalid_streak"] = 0
        return updated, uses

    async def execute(self, uses: list[dict[str, Any]], state: NativeState) -> NativeState:
        """Ferramentas em série, na ordem proposta; gate e término interrompem o restante."""
        results: list[dict[str, Any]] = []
        executed: list[str] = []
        references = list(state.get("references", []))
        stop: str | None = None
        ended: dict[str, Any] | None = None
        terminal: tuple[str, str] | None = None
        for block in uses:
            tool_id, name = str(block["id"]), str(block["name"])
            arguments = cast(dict[str, Any], block["input"])
            if stop is not None:
                results.append(not_executed(tool_id, stop))
                continue
            try:
                outcome = await self.adapter.call(name, arguments)
            except McpTransportError:
                results.append(not_executed(tool_id, "tool_transport_error"))
                raise TerminalSignal("interrupted", "tool_transport_error") from None
            self.write("→ " + summarize(name, outcome))
            data = outcome.data or {}
            if data.get("decision") == AWAITING:
                outcome, stop, terminal = await self.gate(name, arguments, data)
                data = outcome.data or {}
            executed.append(name)
            if name == "submit_mission_plan" and data.get("exploration_allowed") is True:
                state = {**state, "plan_accepted": True}
                display = data.get("display")
                if isinstance(display, str):
                    for line in display.splitlines():
                        self.write(line)
            if name == "end_run" and "error" not in data:
                ended = data
                stop = "run_ended"
            for key in REFERENCE_KEYS:
                if isinstance(data.get(key), str):
                    references.append({"tool": name, key: data[key]})
            results.append(tool_result(tool_id, outcome.text, is_error="error" in data))
            if terminal is None and stop is None:
                terminal = self.run_terminal()
                if terminal is not None:
                    stop = "run_ended_externally"
        return {
            **state,
            "pending_user": results,
            "last_tools": executed,
            "references": references,
            "ended_by_agent": ended,
            "terminal": terminal if ended is None else None,
        }

    async def gate(
        self, name: str, arguments: dict[str, Any], data: dict[str, Any]
    ) -> tuple[ToolOutcome, str | None, tuple[str, str] | None]:
        """Espera humana sem modelo; aprovação repete a chamada original uma única vez.

        Se o servidor recusa o retry por mudança no fingerprint e abre um pedido novo, esse
        pedido é outro gate humano: nada é executado sem a nova decisão.
        """
        for _ in range(MAX_RENEWED_GATES):
            approval_id = str(data.get("approval_id", ""))
            self.write(f"⏸ approval required: {data.get('summary', '')}")
            record = await self.human.decide(approval_id)
            if record.status != ApprovalStatus.APPROVED:
                break
            self.write(f"✓ approved: retrying {name} once")
            try:
                retried = await self.adapter.call(name, {**arguments, "approval_id": approval_id})
            except McpTransportError:
                raise TerminalSignal("interrupted", "tool_transport_error") from None
            self.write("→ " + summarize(name, retried))
            renewed = retried.data or {}
            if renewed.get("decision") != AWAITING or renewed.get("approval_id") == approval_id:
                return retried, None, None
            data = renewed
        else:
            detail = {"decision": "not_executed", "reason": "approval_renewed_too_often"}
            return ToolOutcome(json.dumps(detail), detail, True), "approval_renewed", None
        detail = {
            "decision": "not_executed",
            "approval_id": approval_id,
            "approval_status": record.status.value,
            "reason": record.decision_reason or record.status.value,
        }
        outcome = ToolOutcome(json.dumps(detail), detail, is_error=True)
        self.write(f"✗ not executed: approval {record.status.value}")
        if record.status == ApprovalStatus.ENDED:
            return (
                outcome,
                "run_ended_by_user",
                self.run_terminal() or ("cancelled", "user_cancelled"),
            )
        return outcome, "human_decision_interrupted_batch", None

    async def observation(self, label: str, state: NativeState) -> NativeState:
        try:
            outcome = await self.adapter.call("observe", {})
        except McpTransportError:
            raise TerminalSignal("interrupted", "tool_transport_error") from None
        self.write("→ " + summarize(label, outcome))
        terminal = self.run_terminal()
        if terminal is not None:
            return {**state, "terminal": terminal}
        return {
            **state,
            "pending_user": [*state.get("pending_user", []), tool_data(label, outcome.text)],
        }

    # Nós do grafo -------------------------------------------------------------------------

    async def prepare_mission(self, state: NativeState) -> NativeState:
        if not state.get("messages") and not state.get("pending_user"):
            try:
                context = await self.adapter.call("mission_context", {})
            except McpTransportError:
                raise TerminalSignal("interrupted", "tool_transport_error") from None
            state = {**state, "pending_user": [tool_data("mission_context", context.text)]}
        state, uses = await self.think("prepare_mission", state, PREPARATION_TOOLS)
        return await self.execute(uses, state) if uses else state

    async def consult_memory(self, state: NativeState) -> NativeState:
        state, uses = await self.think("consult_memory", state, MEMORY_TOOLS)
        state = {**state, "memory_consulted": True}
        return await self.execute(uses, state) if uses else state

    async def observe(self, state: NativeState) -> NativeState:
        return await self.observation("observe", state)

    async def decide(self, state: NativeState) -> NativeState:
        previous = state.get("invalid_streak", 0)
        state, uses = await self.think("decide", state, None)
        if not uses and not state.get("pending_user"):
            streak = previous + 1
            if streak >= MAX_INVALID_RESPONSES:
                raise TerminalSignal("interrupted", "agent_protocol_error")
            self.agent_failure("no_tool_call", "response without tool call or end_run")
            return {
                **state,
                "invalid_streak": streak,
                "pending_user": [{"type": "text", "text": "Call a tool, or end_run to finish."}],
                "queue": [],
            }
        return {**state, "queue": uses}

    async def act(self, state: NativeState) -> NativeState:
        uses = state.get("queue", [])
        return {**(await self.execute(uses, state)), "queue": []}

    async def verify(self, state: NativeState) -> NativeState:
        return await self.observation("verify", state)

    async def record(self, state: NativeState) -> NativeState:
        accepted = [ref for ref in state.get("references", []) if ref.get("tool") in RECORD_TOOLS]
        if accepted:
            self.write(f"✓ recorded references: {len(accepted)}")
        return state


def guarded(
    node: Callable[[NativeState], Any],
) -> Callable[[NativeState], Any]:
    async def run(state: NativeState) -> NativeState:
        try:
            return await node(state)
        except TerminalSignal as signal:
            return {**state, "terminal": (signal.status, signal.reason), "queue": []}

    return run


def _finished(state: NativeState) -> bool:
    return bool(state.get("terminal")) or state.get("ended_by_agent") is not None


def route_prepare(state: NativeState) -> str:
    if _finished(state):
        return "wrap_up"
    return "consult_memory" if state.get("plan_accepted") else "prepare_mission"


def route_memory(state: NativeState) -> str:
    return "wrap_up" if _finished(state) else "observe"


def route_observe(state: NativeState) -> str:
    return "wrap_up" if _finished(state) else "decide"


def route_decide(state: NativeState) -> str:
    if _finished(state):
        return "wrap_up"
    return "act" if state.get("queue") else "decide"


def route_act(state: NativeState) -> str:
    if _finished(state):
        return "wrap_up"
    tools = set(state.get("last_tools", []))
    if tools & EFFECT_TOOLS:
        return "verify"
    if tools & RECORD_TOOLS:
        return "record"
    return "decide"


def route_verify(state: NativeState) -> str:
    if _finished(state):
        return "wrap_up"
    return "record" if set(state.get("last_tools", [])) & RECORD_TOOLS else "decide"


def build_graph(agent: NativeAgent, wrap_up: Callable[[NativeState], Any]) -> Any:
    from langgraph.graph import (  # pyright: ignore[reportMissingTypeStubs]
        END,
        START,
        StateGraph,
    )

    # A API do LangGraph é só parcialmente tipada; o contrato dos nós é NativeState.
    graph: Any = StateGraph(NativeState)
    graph.add_node("prepare_mission", guarded(agent.prepare_mission))
    graph.add_node("consult_memory", guarded(agent.consult_memory))
    graph.add_node("observe", guarded(agent.observe))
    graph.add_node("decide", guarded(agent.decide))
    graph.add_node("act", guarded(agent.act))
    graph.add_node("verify", guarded(agent.verify))
    graph.add_node("record", guarded(agent.record))
    graph.add_node("wrap_up", wrap_up)
    graph.add_edge(START, "prepare_mission")
    graph.add_conditional_edges("prepare_mission", route_prepare)
    graph.add_conditional_edges("consult_memory", route_memory)
    graph.add_conditional_edges("observe", route_observe)
    graph.add_conditional_edges("decide", route_decide)
    graph.add_conditional_edges("act", route_act)
    graph.add_conditional_edges("verify", route_verify)
    graph.add_conditional_edges("record", route_observe)
    graph.add_edge("wrap_up", END)
    return graph.compile()
