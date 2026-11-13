import json
from dataclasses import replace
from typing import Any

from lastro.history.models import EventRecord, EventType
from lastro.history.native import ModelCall
from lastro.observability.projection import EXTERNAL_MODEL_REASON, RunProjection, describe

RUN = "run_20261107_120000_a1b2"


def call(**overrides: Any) -> ModelCall:
    values: dict[str, Any] = {
        "id": "call_1",
        "run_id": RUN,
        "step_index": 3,
        "attempt": 2,
        "node": "decide",
        "model": "claude-sonnet-5-5",
        "status": "settled",
        "payload_sha256": "c" * 64,
        "reserved_nusd": 50_000_000,
        "actual_nusd": 12_345_000,
        "input_tokens": 1000,
        "cache_read_tokens": 400,
        "cache_write_tokens": 100,
        "output_tokens": 50,
        "max_output_tokens": 2048,
        "request_id": "req_secret_provider_id",
        "started_at": "2026-11-07T12:00:58.000000Z",
        "finished_at": "2026-11-07T12:01:00.000000Z",
        "latency_ms": 1500,
        "tool_calls": (("toolu_x", "observe"),),
        "error_code": None,
        "completion_event_id": 11,
    }
    values.update(overrides)
    return ModelCall(**values)


def event(event_id: int, kind: EventType, payload: Any = None) -> EventRecord:
    return EventRecord(
        id=event_id,
        run_id=RUN,
        sequence=event_id,
        timestamp="2026-11-07T12:01:00.000000Z",
        event_type=kind,
        tool_name=None,
        arguments_sha256=None,
        arguments_preview=None,
        policy_decision=None,
        correlation_id=None,
        idempotency_key=None,
        payload=payload,
        result=None,
    )


def run(executor: str = "native", calls: tuple[ModelCall, ...] = ()) -> RunProjection:
    return RunProjection(
        project_id="3f0c9a52-6b1e-4d8e-9a77-2a4f1c0d5e61",
        run_id=RUN,
        executor=executor,
        started_at="2026-11-07T12:00:00.000000Z",
        ended_at=None,
        lastro_version="0.1.0",
        root_event_id=1,
        model_calls=calls,
    )


def test_only_terminal_event_counts_usage() -> None:
    projection = run(calls=(call(),))
    started = describe(event(10, EventType.MODEL_CALL_STARTED, {"call_id": "call_1"}), projection)
    completed = describe(event(11, EventType.MODEL_CALL_COMPLETED), projection)

    assert "langfuse.observation.usage_details" not in started.attributes
    assert "langfuse.observation.cost_details" not in started.attributes
    assert started.attributes["lastro.model.step_index"] == 3
    assert completed.attributes["langfuse.observation.type"] == "generation"
    assert completed.attributes["langfuse.observation.model.name"] == "claude-sonnet-5-5"
    assert completed.attributes["lastro.model.attempt"] == 2
    assert completed.attributes["lastro.duration.ms"] == 1500
    assert completed.attributes["lastro.duration.status"] == "measured"
    assert completed.start_time == "2026-11-07T12:00:58.500000Z"
    assert "req_secret_provider_id" not in json.dumps(completed.as_json())


def test_cache_buckets_are_exclusive() -> None:
    completed = describe(event(11, EventType.MODEL_CALL_COMPLETED), run(calls=(call(),)))
    usage = json.loads(str(completed.attributes["langfuse.observation.usage_details"]))

    assert usage == {"input": 1000, "input_cache_read": 400, "input_cache_write": 100, "output": 50}
    assert completed.attributes["lastro.model.total_input"] == 1500


def test_reserved_cost_is_not_actual_cost() -> None:
    completed = describe(event(11, EventType.MODEL_CALL_COMPLETED), run(calls=(call(),)))

    assert completed.attributes["lastro.model.cost_usd"] == "0.012345000"
    assert json.loads(str(completed.attributes["langfuse.observation.cost_details"])) == {
        "total": 0.012345
    }
    assert completed.attributes["lastro.model.reserved_cost_usd"] == "0.050000000"


def test_uncertain_usage_is_not_zero() -> None:
    uncertain = call(
        status="uncertain",
        actual_nusd=None,
        input_tokens=None,
        cache_read_tokens=None,
        cache_write_tokens=None,
        output_tokens=None,
        error_code="provider_timeout",
    )
    failed = describe(event(11, EventType.MODEL_CALL_FAILED), run(calls=(uncertain,)))

    assert failed.attributes["lastro.model_data.status"] == "partial"
    assert failed.attributes["lastro.model.usage_status"] == "uncertain"
    assert failed.attributes["lastro.operation.status"] == "failed"
    for key in (
        "langfuse.observation.usage_details",
        "langfuse.observation.cost_details",
        "langfuse.observation.type",
        "lastro.model.cost_usd",
    ):
        assert key not in failed.attributes


def test_external_executor_model_data_is_unavailable() -> None:
    for executor in ("codex", "claude-code"):
        descriptor = describe(event(5, EventType.ORACLE_SIGNAL), run(executor=executor))
        assert descriptor.attributes["lastro.model_data.status"] == "unavailable"
        assert descriptor.attributes["lastro.model_data.reason"] == EXTERNAL_MODEL_REASON
        assert not any("usage" in key or "cost" in key for key in descriptor.attributes)


def test_null_completion_event_does_not_create_span() -> None:
    orphan = replace(call(), completion_event_id=None, status="reserved")
    projection = run(calls=(orphan,))
    other = describe(event(11, EventType.MODEL_CALL_COMPLETED), projection)

    assert other.attributes["lastro.model_data.status"] == "unavailable"
    assert "langfuse.observation.usage_details" not in other.attributes
    started = describe(event(10, EventType.MODEL_CALL_STARTED, {"call_id": "call_1"}), projection)
    assert started.attributes["lastro.model.usage_status"] == "pending"
