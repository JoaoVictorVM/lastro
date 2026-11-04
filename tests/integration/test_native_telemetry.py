import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from lastro.history import EventType, ReportKind
from lastro.native.pricing import Usage
from lastro.native.provider import ProviderError
from lastro.native.telemetry import model_calls, totals

PLAN: dict[str, Any] = {
    "objectives": ["Conferir a lista"],
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
SECRET = "sk-ant-test-0000000000000000"


def mission(scripted: Any) -> list[Any]:
    return [
        lambda request: scripted.tool_use("submit_mission_plan", PLAN),
        lambda request: [{"type": "text", "text": "No memory needed."}],
        lambda request: scripted.tool_use("screenshot", {}),
        lambda request: scripted.tool_use(
            "end_run", {"outcome": "completed", "reason": "Mission finished"}
        ),
    ]


def run_with_flaky_call(
    make_native_demo: Callable[..., Any],
    scripted: Any,
    run_native: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Uma tentativa com timeout (usage incerto) seguida de retry bem-sucedido."""
    project = make_native_demo()
    model = scripted.Model(mission(scripted))
    original = model.create
    failed: list[bool] = []

    async def create(request: Any) -> Any:
        if len(model.requests) == 2 and not failed:
            failed.append(True)
            raise ProviderError("transient", "timeout")
        return await original(request)

    async def no_wait(delay: float) -> None:
        return None

    model.create = create
    from lastro.native import runner as native_runner
    from lastro.native.provider import ModelGateway

    monkeypatch.setattr(
        native_runner,
        "default_gateway",
        lambda provider, ledger: ModelGateway(provider, ledger, sleep=no_wait),
    )
    return run_native(project, model)


def test_each_api_attempt_has_durable_telemetry(
    make_native_demo: Callable[..., Any],
    scripted: Any,
    run_native: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = run_with_flaky_call(make_native_demo, scripted, run_native, monkeypatch)
    assert native.outcome.status == "completed"
    calls = model_calls(native.history, native.run_id)
    assert [(call.node, call.attempt, call.usage_status) for call in calls] == [
        ("prepare_mission", 1, "reported"),
        ("consult_memory", 1, "reported"),
        ("decide", 1, "uncertain"),
        ("decide", 2, "reported"),
        ("decide", 1, "reported"),
    ]
    events = native.history.list_events(native.run_id)
    started = [e for e in events if e.event_type == EventType.MODEL_CALL_STARTED]
    completed = [e for e in events if e.event_type == EventType.MODEL_CALL_COMPLETED]
    failed = [e for e in events if e.event_type == EventType.MODEL_CALL_FAILED]
    assert len(started) == 5 and len(completed) == 4 and len(failed) == 1
    assert {call.event_id for call in calls} == {event.id for event in completed + failed}
    assert all(e.correlation_id is None for e in started + completed + failed)
    assert all(isinstance(e.payload, dict) and e.payload["call_id"] for e in completed)
    from lastro.cli.tail_cmd import format_event

    lines = [format_event(e, "America/Sao_Paulo") for e in events]
    assert any("native executor started (catalog" in line for line in lines)
    failed_line = "model decide attempt 1 failed (timeout); usage uncertain"
    assert any(failed_line in line for line in lines)
    assert any("model prepare_mission in 1500 out 50 USD" in line for line in lines)


def test_f18_consumer_receives_nullable_usage_and_tool_calls(
    make_native_demo: Callable[..., Any],
    scripted: Any,
    run_native: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = run_with_flaky_call(make_native_demo, scripted, run_native, monkeypatch)
    calls = model_calls(native.history, native.run_id)
    uncertain = next(call for call in calls if call.usage_status == "uncertain")
    assert (uncertain.input_tokens, uncertain.output_tokens, uncertain.cost_usd) == (None,) * 3
    assert uncertain.latency_ms is not None and uncertain.error_code == "timeout"
    reported = [call for call in calls if call.usage_status == "reported"]
    for call in reported:
        assert call.model == "claude-sonnet-5-5"
        assert call.total_input_tokens == 1500
        assert call.output_tokens == 50
        assert call.cost_usd is not None and call.latency_ms is not None
        assert call.request_id is not None and call.event_id is not None
    assert [name for call in reported for _, name in call.tool_calls] == [
        "submit_mission_plan",
        "screenshot",
        "end_run",
    ]
    summary = totals(calls)
    assert (summary.calls, summary.reported_calls, summary.uncertain_calls) == (5, 4, 1)


def test_report_and_summary_read_same_totals(
    make_native_demo: Callable[..., Any],
    scripted: Any,
    run_native: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lastro.native.runner import summary_lines

    native = run_with_flaky_call(make_native_demo, scripted, run_native, monkeypatch)
    usage = native.history.native_usage(native.run_id)
    summary = totals(model_calls(native.history, native.run_id))
    assert summary.total_input_tokens == usage.total_input_tokens
    assert native.outcome.usage.confirmed_usd == summary.cost_usd
    lines = "\n".join(summary_lines(native.outcome))
    assert f"input {usage.total_input_tokens}" in lines
    assert "1 attempt(s) with unknown usage, not counted as zero" in lines
    finals = [
        s for s in native.history.list_report_snapshots(native.run_id) if s.kind == ReportKind.FINAL
    ]
    assert finals
    report = native.runner.outcome.report_paths[0]
    text = Path(report).read_text(encoding="utf-8")
    assert "## Model usage" in text
    assert f"Input tokens: {usage.total_input_tokens}" in text
    assert f"Confirmed cost: USD {summary.cost_usd}" in text
    assert "not counted as zero" in text


def test_api_key_and_headers_never_persist(
    make_native_demo: Callable[..., Any], scripted: Any, run_native: Callable[..., Any]
) -> None:
    project = make_native_demo()
    model = scripted.Model(mission(scripted), usage=Usage(10, 0, 0, 5))
    native = run_native(project, model)
    data_dir = project.context().project_data_dir
    for path in data_dir.rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            assert SECRET.encode() not in content, path
            assert b"x-api-key" not in content.lower(), path
    events = json.dumps([e.payload for e in native.history.list_events(native.run_id)])
    assert "Lastro native executor" not in events
    assert SECRET not in events
