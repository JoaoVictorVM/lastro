import base64
import time
from collections.abc import Callable
from typing import Any

from lastro.config.loader import ProjectContext
from lastro.config.observability import Destination
from lastro.history import RunStatus, TextMission, open_history
from lastro.observability import settings_source
from lastro.observability.exporter import LangfuseSpanExporter
from lastro.observability.projection import EXTERNAL_MODEL_REASON
from lastro.observability.worker import ExportWorker

from .obs_support import Reply, closed_port_base, otlp_receiver, partial_reply

RETRYABLE_CODES = ("export_unavailable", "export_timeout")
PUBLIC = "-".join(("pk", "lf", "sentinel", "public"))
SECRET = "-".join(("sk", "lf", "sentinel", "secret"))


def destination(base: str) -> Destination:
    return Destination(base + "/api/public/otel/v1/traces", base, "proj-lastro-test")


def test_real_http_exporter_encodes_safe_attributes(
    obs_project: Callable[..., ProjectContext],
) -> None:
    with otlp_receiver() as receiver:
        project = obs_project(receiver.base, executor="codex")
        history: Any = open_history(project)
        run = history.create_run(project, TextMission("Mission text SENTINEL-MISSION"))
        history.append_event(
            run.record.id,
            "tool_call",
            tool_name="fill",
            correlation_id="a1",
            arguments={"value": "SENTINEL-TYPED-VALUE", "password": SECRET},
        )
        history.append_event(
            run.record.id,
            "policy_decision",
            tool_name="fill",
            correlation_id="a1",
            policy_decision="deny",
            payload={"reason": "SENTINEL-REASON"},
        )
        history.finish_run(run.record.id, RunStatus.COMPLETED)
        ExportWorker(history, run.record.id, settings_source(project)).drain()

    request = receiver.received[0]
    assert request.path == "/api/public/otel/v1/traces"
    assert request.headers["content-type"] == "application/x-protobuf"
    assert request.headers["x-langfuse-ingestion-version"] == "4"
    expected = base64.b64encode(f"{PUBLIC}:{SECRET}".encode()).decode()
    assert request.headers["authorization"] == f"Basic {expected}"
    body = b"".join(item.body for item in receiver.received)
    for sentinel in (PUBLIC, SECRET, "SENTINEL-MISSION", "SENTINEL-TYPED-VALUE", "SENTINEL-REASON"):
        assert sentinel.encode() not in body
    spans = receiver.span_attributes()
    assert {item["lastro.model_data.reason"] for item in spans} == {EXTERNAL_MODEL_REASON}
    fill = next(item for item in spans if item.get("lastro.tool.name") == "fill")
    assert fill["lastro.operation.status"] == "not_executed"
    assert all(item["langfuse.trace.metadata.run_id"] == run.record.id for item in spans)


def test_redirect_is_not_followed() -> None:
    with otlp_receiver([Reply(status=307, headers={"location": "/elsewhere"})]) as receiver:
        outcome = LangfuseSpanExporter(destination(receiver.base), PUBLIC, SECRET).send([])

    assert (outcome.kind, outcome.code) == ("blocked", "export_rejected")
    assert [item.path for item in receiver.received] == ["/api/public/otel/v1/traces"]


def test_sdk_internal_retry_respects_total_timeout() -> None:
    with otlp_receiver([Reply(delay=3.0)]) as receiver:
        exporter = LangfuseSpanExporter(destination(receiver.base), PUBLIC, SECRET, timeout=0.5)
        started = time.monotonic()
        outcome = exporter.send([])
        elapsed = time.monotonic() - started

    assert (outcome.kind, outcome.code) == ("retry", "export_timeout")
    assert elapsed < 2.5
    assert len(receiver.received) == 1


def test_partial_rejection_preserves_descriptors_without_retry(
    obs_project: Callable[..., ProjectContext],
) -> None:
    with otlp_receiver([partial_reply(1)]) as receiver:
        project = obs_project(receiver.base)
        history: Any = open_history(project)
        run = history.create_run(project, TextMission("Explore employees"))
        history.append_event(run.record.id, "oracle_signal", payload={"kind": "console"})
        ExportWorker(history, run.record.id, settings_source(project)).drain()
        history.unblock_observability(run.record.id)
        ExportWorker(history, run.record.id, settings_source(project)).drain(force=True)

    assert len(receiver.received) == 1
    status = history.observability_status(run.record.id)
    assert status.rejected_spans == 1
    rows = history.observability_spans(run.record.id)
    assert rows[1][1] == "rejected" and rows[1][2] is not None


def test_collector_outage_does_not_change_run_status(
    obs_project: Callable[..., ProjectContext],
) -> None:
    project = obs_project(closed_port_base())
    history: Any = open_history(project)
    run = history.create_run(project, TextMission("Explore employees"))
    history.append_event(run.record.id, "oracle_signal", payload={"kind": "console"})
    worker = ExportWorker(history, run.record.id, settings_source(project), poll_seconds=0.05)
    worker.start()
    history.finish_run(run.record.id, RunStatus.COMPLETED)
    result = worker.stop(drain_budget=1.0)

    # Porta recusada vira ConnectError, mas no Windows a recusa leva ~2,4 s (retransmissão do
    # SYN): um envio com orçamento menor termina em timeout. Os dois códigos são recuperáveis.
    assert result.last_code in RETRYABLE_CODES
    assert history.get_run(run.record.id).status == RunStatus.COMPLETED
    status = history.observability_status(run.record.id)
    assert status.pending_spans == 3
    assert status.export_status == "pending"
    assert status.last_error_code in RETRYABLE_CODES
