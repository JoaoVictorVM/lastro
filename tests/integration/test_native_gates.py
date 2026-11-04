import asyncio
from collections.abc import Callable
from typing import Any

from lastro.browser.tools import BrowserTools
from lastro.history import (
    ApprovalStatus,
    EventType,
    PolicyDecision,
    RunStatus,
    TextMission,
    open_history,
)

PLAN: dict[str, Any] = {
    "objectives": ["Editar o e-mail de um funcionário"],
    "scope": ["Edição de funcionário"],
    "criteria": [{"text": "O e-mail editado é salvo", "source": "mission_explicit"}],
    "gaps": [],
    "out_of_scope": [],
    "charter": {
        "target": "employee edit",
        "resources": ["demo application"],
        "information": "persistence defects",
        "heuristics": ["persistence_after_reload"],
    },
}
EDIT = "/employees/1/edit"
EMAIL = {"target": {"label": "E-mail"}, "value": "qa@example.com", "reason": "Check e-mail"}
SAVE = {"target": {"role": "button", "name": "Salvar"}, "reason": "Save e-mail"}
END = ("end_run", {"outcome": "completed", "reason": "done"})


def script(scripted: Any, base: str, *actions: tuple[str, dict[str, Any]]) -> list[Any]:
    steps: list[Any] = [
        lambda request: scripted.tool_use("submit_mission_plan", PLAN),
        lambda request: [{"type": "text", "text": "No memory needed."}],
        lambda request: scripted.tool_use("navigate", {"url": base + EDIT}),
    ]
    for name, arguments in actions:
        steps.append(lambda request, n=name, a=arguments: scripted.tool_use(n, a))
    steps.append(lambda request: scripted.tool_use(*END))
    return steps


def writes(demo_server: Any) -> list[tuple[str, str]]:
    return [item for item in demo_server.access_log if item == ("POST", "/employees/1")]


def decisions(history: Any, run_id: str, tool: str) -> list[tuple[str, object]]:
    return [
        (
            str(event.policy_decision.value if event.policy_decision else ""),
            event.payload.get("reason") if isinstance(event.payload, dict) else None,
        )
        for event in history.list_events(run_id)
        if event.event_type == EventType.POLICY_DECISION and event.tool_name == tool
    ]


def test_same_deny_and_gate_scenarios_as_claude(
    make_native_demo: Callable[..., Any],
    scripted: Any,
    run_native: Callable[..., Any],
    demo_server: Any,
) -> None:
    project = make_native_demo(policy=True)
    base = project.context().profile.target_url
    destructive = {"target": {"role": "button", "name": "Excluir"}}
    outside = {"url": "https://evil.example/employees"}
    model = scripted.Model(
        script(scripted, base, ("click", destructive), ("navigate", outside), ("fill", EMAIL))
    )
    native = run_native(project, model, ("skip",))
    assert native.outcome.status == "completed"
    native_click = decisions(native.history, native.run_id, "click")
    native_navigate = decisions(native.history, native.run_id, "navigate")
    native_fill = decisions(native.history, native.run_id, "fill")

    # Mesmas chamadas pelo caminho dos executores externos: o mesmo servidor de políticas.
    context = project.context().with_executor("claude-code")
    history = open_history(context)
    run_id = history.create_run(context, TextMission("Explore")).record.id
    runtime = BrowserTools(context, history, run_id)

    async def external() -> None:
        try:
            await runtime.call("submit_mission_plan", PLAN)
            await runtime.call("navigate", {"url": base + EDIT})
            await runtime.call("click", destructive)
            await runtime.call("navigate", outside)
            await runtime.call("fill", EMAIL)
        finally:
            runtime.approvals.close()
            await runtime.session.close()

    asyncio.run(external())
    assert native_click == decisions(history, run_id, "click") == [("deny", "no-destructive")]
    assert native_navigate == decisions(history, run_id, "navigate")
    assert ("deny", "outside_target") in native_navigate
    assert [item[0] for item in native_fill] == [
        item[0] for item in decisions(history, run_id, "fill")
    ]
    assert native_fill[0][0] == PolicyDecision.REQUIRE_APPROVAL.value
    assert not any("evil.example" in path for _, path in demo_server.access_log)
    assert writes(demo_server) == []


def test_native_human_approval_consumed_once(
    make_native_demo: Callable[..., Any],
    scripted: Any,
    run_native: Callable[..., Any],
    demo_server: Any,
) -> None:
    project = make_native_demo(policy=True)
    base = project.context().profile.target_url
    model = scripted.Model(script(scripted, base, ("fill", EMAIL), ("submit", SAVE)))
    native = run_native(project, model, ("approve", "approve"))
    assert native.outcome.status == "completed", native.output
    approvals = native.history.list_approvals(native.run_id)
    assert [item.status for item in approvals] == [ApprovalStatus.CONSUMED] * 2
    assert all(item.decided_by is not None and item.decided_by.value == "cli" for item in approvals)
    assert len(writes(demo_server)) == 1
    assert any("approved: retrying submit once" in line for line in native.output)


def test_changed_fingerprint_requires_new_gate() -> None:
    from pathlib import Path

    from lastro.config.native import NativeSettings
    from lastro.native.graph import NativeAgent
    from lastro.native.mcp import McpSessionAdapter, ToolOutcome, expected_catalog

    class Adapter(McpSessionAdapter):
        def __init__(self) -> None:
            super().__init__(project_dir=Path("."), config_path=Path("x"), profile="p", run_id="r")
            self.catalog = expected_catalog()
            self.replies = [
                {"decision": "awaiting_approval", "approval_id": "act2", "summary": "changed"},
                {"status": "ok", "approval_status": "consumed"},
            ]
            self.calls: list[dict[str, Any]] = []

        async def call(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
            self.calls.append(arguments)
            data = self.replies.pop(0)
            import json

            return ToolOutcome(json.dumps(data), data, False)

    class Human:
        def __init__(self) -> None:
            self.asked: list[str] = []

        async def decide(self, approval_id: str) -> Any:
            self.asked.append(approval_id)
            return type("R", (), {"status": ApprovalStatus.APPROVED, "decision_reason": None})()

    adapter, human = Adapter(), Human()
    agent = NativeAgent(
        history=None,  # type: ignore[arg-type]
        run_id="r",
        adapter=adapter,
        gateway=None,  # type: ignore[arg-type]
        human=human,  # type: ignore[arg-type]
        settings=NativeSettings(),
        system=[],
        write=lambda line: None,
    )
    awaiting = {"decision": "awaiting_approval", "approval_id": "act1", "summary": "edit"}
    outcome, stop, terminal = asyncio.run(agent.gate("fill", dict(EMAIL), awaiting))
    assert human.asked == ["act1", "act2"]
    assert [call["approval_id"] for call in adapter.calls] == ["act1", "act2"]
    assert outcome.data == {"status": "ok", "approval_status": "consumed"}
    assert (stop, terminal) == (None, None)


def test_skipped_or_expired_gate_has_no_effect(
    make_native_demo: Callable[..., Any],
    scripted: Any,
    run_native: Callable[..., Any],
    demo_server: Any,
) -> None:
    project = make_native_demo(policy=True)
    base = project.context().profile.target_url
    model = scripted.Model(script(scripted, base, ("fill", EMAIL), ("submit", SAVE)))
    native = run_native(project, model, ("skip", "skip"))
    approvals = native.history.list_approvals(native.run_id)
    assert [item.status for item in approvals] == [ApprovalStatus.SKIPPED] * 2
    assert writes(demo_server) == []
    assert native.history.get_run(native.run_id).status == RunStatus.COMPLETED
    assert sum("not executed: approval skipped" in line for line in native.output) == 2
