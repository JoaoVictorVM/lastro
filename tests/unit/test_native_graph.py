import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

pytest.importorskip("langgraph", reason="native extra not installed")

from lastro.config.loader import ProjectContext
from lastro.config.native import NativeSettings
from lastro.history import ApprovalStatus, EventType, TextMission, open_history
from lastro.native.graph import NativeAgent, build_graph, response_problem
from lastro.native.mcp import McpSessionAdapter, ToolOutcome, expected_catalog
from lastro.native.models import NativeState
from lastro.native.pricing import Usage
from lastro.native.provider import ModelResponse

PLAN: dict[str, Any] = {
    "objectives": ["Conferir a lista"],
    "scope": ["Lista"],
    "criteria": [{"text": "A lista aparece", "source": "mission_explicit"}],
    "gaps": [],
    "out_of_scope": [],
    "charter": {
        "target": "employee list",
        "resources": ["demo"],
        "information": "display defects",
        "heuristics": ["crud_consistency"],
    },
}
FINDING: dict[str, Any] = {
    "title": "Telefone não persiste",
    "flow": "Cadastro",
    "steps": ["Abrir", "Salvar"],
    "expected": {"text": "O telefone permanece", "source": "heuristic"},
    "observed": "O campo aparece vazio.",
    "classification": "suspected",
    "failure_domain": "system",
}
PLAN_RESULT = {"plan_id": "plan_1", "exploration_allowed": True, "display": "Mission plan"}


class FakeAdapter(McpSessionAdapter):
    def __init__(self, results: dict[str, Any] | None = None) -> None:
        super().__init__(
            project_dir=Path("."), config_path=Path("lastro.toml"), profile="p", run_id="r"
        )
        self.catalog = expected_catalog()
        self.results = results or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        self.calls.append((name, arguments))
        value = self.results.get(name, {"status": "ok"})
        if callable(value):
            value = value(arguments)
        return ToolOutcome(json.dumps(value), cast(dict[str, Any], value), False)


class FakeGateway:
    def __init__(self, script: list[list[dict[str, Any]] | ModelResponse]) -> None:
        self.script = script
        self.nodes: list[str] = []
        self.requests: list[Any] = []

    async def call(self, node: str, step: int, request: Any, **kwargs: Any) -> ModelResponse:
        self.nodes.append(node)
        self.requests.append(request)
        item = self.script.pop(0)
        if isinstance(item, ModelResponse):
            return item
        stop = "tool_use" if any(b["type"] == "tool_use" for b in item) else "end_turn"
        return ModelResponse(item, stop, Usage(1, 0, 0, 1))


class FakeHuman:
    def __init__(self, status: ApprovalStatus) -> None:
        self.status = status
        self.asked: list[str] = []

    async def decide(self, approval_id: str) -> Any:
        self.asked.append(approval_id)
        return type("Record", (), {"status": self.status, "decision_reason": None})()


def use(name: str, arguments: dict[str, Any] | None = None, tool_id: str = "") -> dict[str, Any]:
    return {
        "type": "tool_use",
        "id": tool_id or f"toolu_{name}_{abs(hash((name, json.dumps(arguments))))}",
        "name": name,
        "input": arguments if arguments is not None else {},
    }


def text(value: str = "ok") -> dict[str, Any]:
    return {"type": "text", "text": value}


END = [use("end_run", {"outcome": "completed", "reason": "done"}, "toolu_end")]
PREPARE = [
    [use("submit_mission_plan", PLAN, "toolu_plan")],
    [text("no memory")],
]


def run_graph(
    context: ProjectContext,
    script: list[Any],
    results: dict[str, Any] | None = None,
    human: FakeHuman | None = None,
) -> tuple[NativeState, FakeAdapter, FakeGateway, Any, str]:
    native = context.with_executor("native")
    history = open_history(native)
    run_id = history.create_run(native, TextMission("m")).record.id
    adapter = FakeAdapter({"submit_mission_plan": PLAN_RESULT, **(results or {})})
    gateway = FakeGateway(script)
    agent = NativeAgent(
        history=history,
        run_id=run_id,
        adapter=adapter,
        gateway=gateway,  # type: ignore[arg-type]
        human=human or FakeHuman(ApprovalStatus.APPROVED),  # type: ignore[arg-type]
        settings=NativeSettings(),
        system=[text("system")],
        write=lambda line: None,
        tools=[tool.document() for tool in adapter.catalog],
    )
    final: list[NativeState] = []

    async def wrap_up(state: NativeState) -> NativeState:
        final.append(state)
        return state

    asyncio.run(build_graph(agent, wrap_up).ainvoke({}, config={"recursion_limit": 512}))
    if not history.get_run(run_id).status.is_terminal:
        history.finish_run(run_id, "completed")
    return final[0], adapter, gateway, history, run_id


def names(adapter: FakeAdapter) -> list[str]:
    return [name for name, _ in adapter.calls]


def test_eight_nodes_follow_preparation_gate(history_context: ProjectContext) -> None:
    script = [
        [use("observe", {}, "toolu_early")],
        *PREPARE,
        [use("click", {"target": {"role": "button", "name": "Salvar"}}, "toolu_click")],
        [use("report_finding", FINDING, "toolu_finding")],
        END,
    ]
    state, adapter, gateway, history, run_id = run_graph(
        history_context, script, {"report_finding": {"finding_id": "finding_1"}}
    )
    assert gateway.nodes == [
        "prepare_mission",
        "prepare_mission",
        "consult_memory",
        "decide",
        "decide",
        "decide",
    ]
    assert names(adapter) == [
        "mission_context",
        "submit_mission_plan",
        "observe",
        "click",
        "observe",
        "report_finding",
        "end_run",
    ]
    assert state.get("ended_by_agent") is not None
    assert {"tool": "report_finding", "finding_id": "finding_1"} in state.get("references", [])
    failures = [e for e in history.list_events(run_id) if e.event_type == EventType.AGENT_FAILURE]
    assert len(failures) == 1


def test_tool_results_preserve_ids_and_order(history_context: ProjectContext) -> None:
    batch = [use("screenshot", {}, "toolu_a"), use("observe", {}, "toolu_b")]
    script = [*PREPARE, batch, END]
    _, adapter, gateway, _, _ = run_graph(history_context, script)
    assert names(adapter)[-3:] == ["screenshot", "observe", "end_run"]
    last_user = gateway.requests[-1].messages[-1]
    results = [b["tool_use_id"] for b in last_user["content"] if b["type"] == "tool_result"]
    assert results == ["toolu_a", "toolu_b"]
    roles = [message["role"] for message in gateway.requests[-1].messages]
    assert roles == (["user", "assistant"] * len(roles))[: len(roles)]
    assert roles[-1] == "user"


def test_unknown_tool_is_agent_failure(history_context: ProjectContext) -> None:
    script = [*PREPARE, [use("shell", {"cmd": "dir"}, "toolu_shell")], END]
    _, adapter, gateway, history, run_id = run_graph(history_context, script)
    assert "shell" not in names(adapter)
    content = gateway.requests[-1].messages[-1]["content"]
    result = next(b for b in content if b.get("tool_use_id") == "toolu_shell")
    assert result["is_error"] is True
    assert "Unknown tool: shell" in result["content"]
    failures = [e for e in history.list_events(run_id) if e.event_type == EventType.AGENT_FAILURE]
    assert failures and failures[0].payload["code"] == "invalid_tool_call"
    assert history.get_run(run_id).status.value == "completed"


def test_terminal_state_stops_remaining_batch(history_context: ProjectContext) -> None:
    awaiting = {"decision": "awaiting_approval", "approval_id": "act1", "summary": "edit"}
    batch = [
        use("click", {"target": {"role": "button", "name": "Salvar"}}, "toolu_gate"),
        use("fill", {"target": {"label": "Telefone"}, "value": "1"}, "toolu_after"),
    ]
    human = FakeHuman(ApprovalStatus.SKIPPED)
    _, adapter, gateway, _, _ = run_graph(
        history_context, [*PREPARE, batch, END], {"click": awaiting}, human
    )
    assert human.asked == ["act1"]
    assert "fill" not in names(adapter)
    content = gateway.requests[-1].messages[-1]["content"]
    after = next(b for b in content if b.get("tool_use_id") == "toolu_after")
    assert "not_executed" in after["content"]

    approved = FakeHuman(ApprovalStatus.APPROVED)
    _, adapter, _, _, _ = run_graph(
        history_context, [*PREPARE, batch[:1], END], {"click": awaiting}, approved
    )
    clicks = [arguments for name, arguments in adapter.calls if name == "click"]
    assert clicks[1]["approval_id"] == "act1"
    assert len(clicks) == 2


def test_incomplete_response_never_dispatches_tools(history_context: ProjectContext) -> None:
    truncated = ModelResponse(
        [use("click", {"target": {"role": "button", "name": "Salvar"}}, "toolu_cut")],
        "max_tokens",
        Usage(1, 0, 0, 256),
    )
    assert response_problem(truncated, seen_ids=set()) == "incomplete_response"
    duplicated = ModelResponse([use("observe", {}, "toolu_1")], "tool_use", Usage(1, 0, 0, 2))
    assert response_problem(duplicated, seen_ids={"toolu_1"}) == "duplicate_tool_use_id"
    _, adapter, _, _, _ = run_graph(history_context, [*PREPARE, truncated, END])
    assert "click" not in names(adapter)


def test_three_invalid_responses_interrupt(history_context: ProjectContext) -> None:
    bad = [use("shell", {}, f"toolu_bad_{index}") for index in range(3)]
    state, adapter, _, _, _ = run_graph(history_context, [*PREPARE, *[[b] for b in bad]])
    assert state.get("terminal") == ("interrupted", "agent_protocol_error")
    assert "shell" not in names(adapter)


@pytest.mark.parametrize("bad", [{"approval_proof": "forged"}, {"unexpected": True}])
def test_model_supplied_proof_or_invalid_schema_is_rejected(
    history_context: ProjectContext, bad: dict[str, Any]
) -> None:
    arguments = {"target": {"role": "button", "name": "Salvar"}, **bad}
    _, adapter, _, _, _ = run_graph(history_context, [*PREPARE, [use("click", arguments)], END])
    assert "click" not in names(adapter)
