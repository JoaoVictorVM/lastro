import asyncio
import time
from collections.abc import Callable
from typing import Any

import pytest

from lastro.browser.control import control_directory
from lastro.history import ApprovalStatus, ReportKind, RunStatus

PLAN: dict[str, Any] = {
    "objectives": ["Conferir a lista de funcionários"],
    "scope": ["Lista"],
    "criteria": [{"text": "A lista aparece", "source": "mission_explicit"}],
    "gaps": [],
    "out_of_scope": [],
    "charter": {
        "target": "employee list",
        "resources": ["demo application"],
        "information": "display defects",
        "heuristics": ["crud_consistency"],
    },
}


def opening(scripted: Any) -> list[Any]:
    return [
        lambda request: scripted.tool_use("submit_mission_plan", PLAN),
        lambda request: [{"type": "text", "text": "No memory needed."}],
    ]


def partials(history: Any, run_id: str) -> list[Any]:
    return [s for s in history.list_report_snapshots(run_id) if s.kind == ReportKind.PARTIAL]


def owner_gone(project: Any, run_id: str, timeout: float = 15.0) -> bool:
    directory = control_directory(project.context().project_data_dir, run_id)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not (directory / "owner.json").exists():
            return True
        time.sleep(0.1)
    return False


def test_cost_cap_locks_owner_and_builds_partial(
    make_native_demo: Callable[..., Any], scripted: Any, run_native: Callable[..., Any]
) -> None:
    # Reserva por chamada: (1500 + 1024) * 2500 + 2048 * 10000 = 26.79M nUSD; custo real de
    # cada chamada liquidada: 2.79M. Cabem três reservas; a quarta ultrapassa USD 0.034.
    project = make_native_demo(native='cost_cap_usd = "0.034"')
    model = scripted.Model(
        [*opening(scripted), lambda request: scripted.tool_use("screenshot", {})]
    )
    native = run_native(project, model)
    record = native.history.get_run(native.run_id)
    assert (record.status, record.status_reason) == (RunStatus.LIMIT_REACHED, "cost_cap")
    assert len(model.requests) == 3
    assert model.steps == []
    reports = partials(native.history, native.run_id)
    assert len(reports) == 1 and reports[0].termination_reason == "cost_cap"
    shots = [
        a for a in native.history.list_artifacts(native.run_id) if a.relative_path.endswith(".png")
    ]
    assert len(shots) == 1
    usage = native.history.native_usage(native.run_id)
    assert usage.committed_nusd <= usage.cap_nusd
    assert usage.calls == 3
    assert owner_gone(project, native.run_id)
    assert (native.outcome.status, native.outcome.reason) == ("limit_reached", "cost_cap")


def test_sigint_during_api_keeps_uncertain_reservation(
    make_native_demo: Callable[..., Any], scripted: Any, run_native: Callable[..., Any]
) -> None:
    project = make_native_demo()

    def hang(request: Any) -> list[dict[str, Any]]:
        raise AssertionError("replaced below")

    model = scripted.Model([*opening(scripted), hang])
    original = model.create

    async def create(request: Any) -> Any:
        if len(model.requests) == 2:
            model.requests.append(request)
            await asyncio.sleep(60)
        return await original(request)

    model.create = create
    native = run_native(project, model, cancel_after=8.0)
    record = native.history.get_run(native.run_id)
    assert (record.status, record.status_reason) == (RunStatus.CANCELLED, "user_cancelled")
    calls = native.history.list_model_calls(native.run_id)
    assert [call.status for call in calls] == ["settled", "settled", "uncertain"]
    usage = native.history.native_usage(native.run_id)
    assert usage.uncertain_calls == 1
    assert usage.committed_nusd > usage.confirmed_nusd
    assert partials(native.history, native.run_id)
    assert owner_gone(project, native.run_id)


def test_cancel_during_approval_has_no_retry(
    make_native_demo: Callable[..., Any],
    scripted: Any,
    run_native: Callable[..., Any],
    demo_server: Any,
) -> None:
    project = make_native_demo(policy=True)
    base = project.context().profile.target_url
    email = {"target": {"label": "E-mail"}, "value": "qa@example.com", "reason": "Check"}
    model = scripted.Model(
        [
            *opening(scripted),
            lambda request: scripted.tool_use("navigate", {"url": base + "/employees/1/edit"}),
            lambda request: scripted.tool_use("fill", email),
        ]
    )
    native = run_native(project, model, ("end",))
    record = native.history.get_run(native.run_id)
    assert record.status == RunStatus.CANCELLED
    approvals = native.history.list_approvals(native.run_id)
    assert [item.status for item in approvals] == [ApprovalStatus.ENDED]
    fills = [
        e
        for e in native.history.list_events(native.run_id)
        if e.tool_name == "fill" and e.event_type.value == "tool_call"
    ]
    assert len(fills) == 1
    assert len(model.requests) == 4
    assert partials(native.history, native.run_id)
    assert not [
        item for item in demo_server.access_log if item[0] == "POST" and "/employees/" in item[1]
    ]


def test_failed_report_preserves_terminal_state(
    native_demo: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lastro.history import TextMission, open_history
    from lastro.native.prompts import load_bundle
    from lastro.native.runner import NativeRunner, Preflight
    from lastro.reporting.builder import ReportBuilder

    context = native_demo.context()
    history = open_history(context)
    bundle = load_bundle(context.loaded.project_dir)
    run_id = history.create_run(
        context, TextMission("x"), skill_bundle_version=bundle.sha256
    ).record.id

    def broken(self: Any, run: str, reason: str) -> Any:
        raise RuntimeError("template exploded")

    monkeypatch.setattr(ReportBuilder, "build_partial", broken)
    runner = NativeRunner(
        context,
        history,
        run_id,
        Preflight(context.profile.native, "sk-ant-test", bundle),
        None,  # type: ignore[arg-type]
        write=lambda line: None,
        interactive=False,
    )
    outcome = asyncio.run(runner.finish("limit_reached", "cost_cap"))
    assert (outcome.status, outcome.report_status) == ("limit_reached", "failed")
    record = history.get_run(run_id)
    assert (record.status, record.status_reason) == (RunStatus.LIMIT_REACHED, "cost_cap")


def test_killed_child_leaves_no_live_browser(
    make_native_demo: Callable[..., Any],
    scripted: Any,
    run_native: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lastro.native.mcp import McpSessionAdapter, McpTransportError

    original = McpSessionAdapter.call

    async def call(self: McpSessionAdapter, name: str, arguments: dict[str, Any]) -> Any:
        if name == "screenshot":
            await self.close()
            self.lost = True
            raise McpTransportError("child exited")
        return await original(self, name, arguments)

    monkeypatch.setattr(McpSessionAdapter, "call", call)
    project = make_native_demo()
    model = scripted.Model(
        [*opening(scripted), lambda request: scripted.tool_use("screenshot", {})]
    )
    native = run_native(project, model)
    record = native.history.get_run(native.run_id)
    assert (record.status, record.status_reason) == (RunStatus.INTERRUPTED, "tool_transport_error")
    assert owner_gone(project, native.run_id)
    screenshots = [
        e for e in native.history.list_events(native.run_id) if e.tool_name == "screenshot"
    ]
    assert screenshots == []
    assert partials(native.history, native.run_id)
    assert native.history.get_active_run() is None
