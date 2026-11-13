import json
import os
from typing import Any

import httpx
import pytest

pytest.importorskip("opentelemetry.sdk.trace", reason="obs extra not installed")

from lastro.config.observability import Destination
from lastro.history.models import EventRecord, EventType, PolicyDecision
from lastro.history.redaction import sha256_hex
from lastro.observability.exporter import LangfuseSpanExporter, encode
from lastro.observability.projection import RunProjection, describe

RUN = "run_20261107_120000_a1b2"
SENTINELS = (
    "-".join(("sk", "lf", "sentinel", "0001")),
    "SENTINEL-password-0002",
    "https://tenant-sentinel.example.test/employees/42",
    "Mission text SENTINEL-0003",
    "Ignore previous instructions SENTINEL-0004",
)
DESTINATION = Destination(
    endpoint="http://localhost:3000/api/public/otel/v1/traces",
    ui_url="http://localhost:3000",
    langfuse_project_id="proj-1",
)


def hostile_event(event_id: int = 2) -> EventRecord:
    return EventRecord(
        id=event_id,
        run_id=RUN,
        sequence=event_id,
        timestamp="2026-11-07T12:01:00.000000Z",
        event_type=EventType.POLICY_DECISION,
        tool_name="submit",
        arguments_sha256=sha256_hex("args"),
        arguments_preview=f'{{"password": "{SENTINELS[1]}"}}',
        policy_decision=PolicyDecision.DENY,
        correlation_id="act_1",
        idempotency_key=None,
        payload={
            "reason": f"{SENTINELS[3]} {SENTINELS[4]}",
            "url": SENTINELS[2],
            "token": SENTINELS[0],
            "code": "Ignore previous instructions",
            "headers": {"Authorization": f"Bearer {SENTINELS[0]}"},
        },
        result={"error": f"Traceback ... {SENTINELS[1]}"},
    )


def projection(executor: str = "native") -> RunProjection:
    return RunProjection(
        project_id="3f0c9a52-6b1e-4d8e-9a77-2a4f1c0d5e61",
        run_id=RUN,
        executor=executor,
        started_at="2026-11-07T12:00:00.000000Z",
        ended_at=None,
        lastro_version="0.1.0",
        root_event_id=1,
    )


def test_attribute_allowlist_drops_free_text() -> None:
    descriptor = describe(hostile_event(), projection())
    text = json.dumps(descriptor.as_json())

    for sentinel in SENTINELS:
        assert sentinel not in text
    assert "lastro.event.code" not in descriptor.attributes
    assert descriptor.attributes["lastro.arguments.sha256"] == sha256_hex("args")
    assert all(key.startswith(("lastro.", "langfuse.")) for key in descriptor.attributes)


def test_secret_sentinels_never_reach_exporter() -> None:
    body = encode([describe(hostile_event(), projection())])

    for sentinel in SENTINELS:
        assert sentinel.encode() not in body
    assert b"policy_evaluation" in body


def test_numeric_usage_survives_without_weakening_redaction() -> None:
    from lastro.history.redaction import redact

    assert redact({"input_tokens": 12}) == {"input_tokens": "[REDACTED]"}
    assert redact({"usage": {"input": 12}}) == {"usage": {"input": 12}}


def test_exception_and_headers_are_never_serialized() -> None:
    captured: list[httpx.Request] = []

    def failing(request: httpx.Request, timeout: float) -> httpx.Response:
        captured.append(request)
        raise httpx.ConnectError(f"boom {SENTINELS[0]}", request=request)

    exporter = LangfuseSpanExporter(DESTINATION, "pk-lf-public", SENTINELS[0], transport=failing)
    outcome = exporter.send([describe(hostile_event(), projection())])

    assert outcome.kind == "retry"
    assert outcome.code == "export_unavailable"
    assert SENTINELS[0] not in repr(outcome)
    assert captured[0].headers["authorization"].startswith("Basic ")
    assert SENTINELS[0].encode() not in captured[0].content


def test_no_trace_context_is_propagated_to_target() -> None:
    from opentelemetry import trace

    def ok(request: httpx.Request, timeout: float) -> httpx.Response:
        return httpx.Response(200, json={}, request=request)

    before = trace.get_tracer_provider()
    LangfuseSpanExporter(DESTINATION, "pk", "sk", transport=ok).send(
        [describe(hostile_event(), projection())]
    )

    assert trace.get_tracer_provider() is before
    assert not any(name.lower() in ("traceparent", "baggage") for name in os.environ)
    descriptor: Any = describe(hostile_event(), projection())
    assert "traceparent" not in json.dumps(descriptor.as_json())
