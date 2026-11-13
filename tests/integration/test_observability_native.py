import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

from lastro.config.loader import ProjectContext
from lastro.history import (
    NativeContextInput,
    ReservationInput,
    RunStatus,
    Settlement,
    TextMission,
    open_history,
)
from lastro.native.telemetry import model_calls
from lastro.observability import settings_source
from lastro.observability.models import ExportOutcome, SpanDescriptor
from lastro.observability.worker import ExportWorker


class Recorder:
    def __init__(self) -> None:
        self.spans: list[SpanDescriptor] = []

    def send(self, descriptors: list[SpanDescriptor]) -> ExportOutcome:
        self.spans.extend(descriptors)
        return ExportOutcome("ok")


@pytest.fixture
def native_run(obs_project: Callable[..., ProjectContext]) -> tuple[ProjectContext, Any, str]:
    project = obs_project(executor="native")
    history = open_history(project)
    run = history.create_run(project, TextMission("Explore employees"))
    history.start_native_run(
        NativeContextInput(
            run_id=run.record.id,
            cap_nusd=2_000_000_000,
            suite_budget_id=None,
            catalog=[{"name": "observe", "description": "d", "input_schema": {}}],
            catalog_sha256="a" * 64,
            pricing={"models": {}},
            pricing_sha256="b" * 64,
            runtime_versions={"lastro": "0"},
        )
    )
    return project, history, run.record.id


def reserve(history: Any, run_id: str, step: int, attempt: int = 1) -> str:
    call = history.reserve_model_call(
        ReservationInput(
            run_id=run_id,
            step_index=step,
            attempt=attempt,
            node="decide",
            model="claude-sonnet-5-5",
            payload_sha256="c" * 64,
            max_output_tokens=2048,
            reserved_nusd=40_000_000,
        )
    )
    history.mark_model_call_sent(call.id)
    return call.id


def settle(history: Any, call_id: str, actual: int) -> None:
    history.settle_model_call(
        call_id,
        Settlement(
            input_tokens=1000,
            cache_read_tokens=300,
            cache_write_tokens=200,
            output_tokens=80,
            actual_nusd=actual,
            request_id="req_provider_SENTINEL",
            latency_ms=1234,
            tool_calls=(("toolu_1", "observe"),),
            pricing_id="sha256:" + "b" * 64,
        ),
    )


def export(project: ProjectContext, history: Any, run_id: str) -> Recorder:
    recorder = Recorder()
    ExportWorker(
        history, run_id, settings_source(project), sender_factory=lambda *_: recorder
    ).drain()
    return recorder


def terminal(recorder: Recorder) -> list[SpanDescriptor]:
    return [
        item
        for item in recorder.spans
        if item.attributes["lastro.event.type"] in ("model_call_completed", "model_call_failed")
    ]


def test_ledger_telemetry_matches_terminal_span(
    native_run: tuple[ProjectContext, Any, str],
) -> None:
    project, history, run_id = native_run
    settle(history, reserve(history, run_id, step=1), 7_500_000)
    history.finish_run(run_id, RunStatus.COMPLETED)

    recorder = export(project, history, run_id)

    (telemetry,) = model_calls(history, run_id)
    (span,) = terminal(recorder)
    assert span.attributes["lastro.event.id"] == telemetry.event_id
    assert span.attributes["lastro.model.name"] == telemetry.model
    assert span.attributes["lastro.model.step_index"] == telemetry.step_index
    assert span.attributes["lastro.model.attempt"] == telemetry.attempt
    assert span.attributes["lastro.duration.ms"] == telemetry.latency_ms
    assert span.attributes["lastro.model.cost_usd"] == telemetry.cost_usd
    assert json.loads(str(span.attributes["langfuse.observation.usage_details"])) == {
        "input": telemetry.input_tokens,
        "input_cache_read": telemetry.cache_read_input_tokens,
        "input_cache_write": telemetry.cache_creation_input_tokens,
        "output": telemetry.output_tokens,
    }
    assert "req_provider_SENTINEL" not in json.dumps([item.as_json() for item in recorder.spans])


def test_unknown_usage_remains_partial(native_run: tuple[ProjectContext, Any, str]) -> None:
    project, history, run_id = native_run
    call_id = reserve(history, run_id, step=1)
    history.fail_model_call(call_id, released=False, error_code="provider_timeout")
    history.finish_run(run_id, RunStatus.COMPLETED)

    (span,) = terminal(export(project, history, run_id))

    assert span.attributes["lastro.model_data.status"] == "partial"
    assert span.attributes["lastro.model.usage_status"] == "uncertain"
    assert "langfuse.observation.cost_details" not in span.attributes
    assert "langfuse.observation.usage_details" not in span.attributes


def test_native_and_mcp_processes_share_single_trace(
    native_run: tuple[ProjectContext, Any, str],
) -> None:
    project, history, run_id = native_run
    mcp_owner = open_history(project)
    mcp_owner.append_event(run_id, "oracle_signal", payload={"kind": "console"})
    settle(history, reserve(history, run_id, step=1), 1_000_000)
    first, second = Recorder(), Recorder()
    owner_worker = ExportWorker(
        mcp_owner, run_id, settings_source(project), sender_factory=lambda *_: first
    )
    runner_worker = ExportWorker(
        history, run_id, settings_source(project), sender_factory=lambda *_: second
    )

    assert mcp_owner.claim_observability_lease(run_id, owner_worker.token)
    busy = runner_worker.cycle()
    assert busy.last_code == "obs_lease_busy"
    mcp_owner.release_observability_lease(run_id, owner_worker.token)
    history.finish_run(run_id, RunStatus.COMPLETED)
    owner_worker.drain()
    runner_worker.drain()

    spans = first.spans + second.spans
    assert len({item.trace_id for item in spans}) == 1
    assert len({item.span_id for item in spans}) == len(spans) == len(history.list_events(run_id))


def test_cost_is_not_double_counted(native_run: tuple[ProjectContext, Any, str]) -> None:
    project, history, run_id = native_run
    settle(history, reserve(history, run_id, step=1), 2_000_000)
    retry = reserve(history, run_id, step=2, attempt=1)
    history.fail_model_call(retry, released=True, error_code="rate_limited")
    settle(history, reserve(history, run_id, step=2, attempt=2), 3_000_000)
    history.finish_run(run_id, RunStatus.COMPLETED)

    recorder = export(project, history, run_id)

    exported = sum(
        Decimal(str(json.loads(str(item.attributes["langfuse.observation.cost_details"]))["total"]))
        for item in recorder.spans
        if "langfuse.observation.cost_details" in item.attributes
        and item.attributes.get("lastro.model.usage_status") == "reported"
    )
    assert exported == Decimal("0.005")
    root = next(item for item in recorder.spans if item.parent_span_id is None)
    assert not any("cost" in key or "usage" in key for key in root.attributes)
    started = [
        item
        for item in recorder.spans
        if item.attributes["lastro.event.type"] == "model_call_started"
    ]
    assert started and all(
        "langfuse.observation.cost_details" not in item.attributes for item in started
    )
