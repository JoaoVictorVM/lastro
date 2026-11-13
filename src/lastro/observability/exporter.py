"""Adaptador OTLP HTTP/protobuf para o Langfuse, construído sobre `SpanExporter` do SDK.

Os descritores viram `ReadableSpan` com contexto, parent, resource e timestamps explícitos; o
corpo é codificado pelo encoder oficial e enviado em uma única tentativa por lote, dentro do
orçamento total. A resposta é validada aqui (inclusive `partial_success`), porque o exporter
padrão do SDK esconde a rejeição parcial e repete internamente sem respeitar o orçamento.
Redirects nunca são seguidos, para que o Basic Auth não vá a outro host.
"""

import base64
import json
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import cast

import httpx
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import SpanContext, SpanKind, TraceFlags
from opentelemetry.util.types import AttributeValue

from lastro import get_version
from lastro.config.observability import Destination
from lastro.observability.models import ExportOutcome, SpanDescriptor

EXPORT_TIMEOUT_SECONDS = 5.0
INGESTION_VERSION = "4"
RETRYABLE_STATUS = {
    429: "export_rate_limited",
    502: "export_unavailable",
    503: "export_unavailable",
    504: "export_unavailable",
}
UNAUTHORIZED_STATUS = frozenset({401, 403})
_SCOPE = InstrumentationScope("lastro", get_version())

type Transport = Callable[[httpx.Request, float], httpx.Response]


def _nanos(timestamp: str) -> int:
    moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return int(moment.timestamp()) * 1_000_000_000 + moment.microsecond * 1000


def readable_span(descriptor: SpanDescriptor) -> ReadableSpan:
    flags = TraceFlags(TraceFlags.SAMPLED)
    trace_id = int(descriptor.trace_id, 16)
    context = SpanContext(trace_id, int(descriptor.span_id, 16), is_remote=False, trace_flags=flags)
    parent = (
        SpanContext(
            trace_id, int(descriptor.parent_span_id, 16), is_remote=False, trace_flags=flags
        )
        if descriptor.parent_span_id is not None
        else None
    )
    attributes: dict[str, AttributeValue] = {
        key: (tuple(value) if isinstance(value, list) else value)
        for key, value in descriptor.attributes.items()
    }
    return ReadableSpan(
        name=descriptor.name,
        context=context,
        parent=parent,
        resource=Resource(dict(descriptor.resource)),
        attributes=attributes,
        kind=SpanKind.INTERNAL,
        start_time=_nanos(descriptor.start_time),
        end_time=_nanos(descriptor.end_time),
        instrumentation_scope=_SCOPE,
    )


def encode(descriptors: Sequence[SpanDescriptor]) -> bytes:
    return encode_spans([readable_span(item) for item in descriptors]).SerializeToString()


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _rejected_spans(response: httpx.Response) -> int | None:
    """Spans rejeitados na resposta 200; None quando a resposta não é OTLP válida."""
    body = response.content
    if not body.strip():
        return 0
    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
        try:
            data = json.loads(body)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        partial = cast(dict[str, object], data).get("partialSuccess") or cast(
            dict[str, object], data
        ).get("partial_success")
        if partial is None:
            return 0
        if not isinstance(partial, dict):
            return None
        fields = cast(dict[str, object], partial)
        raw = fields.get("rejectedSpans", fields.get("rejected_spans", 0))
        try:
            return int(cast(int | str, raw))
        except (TypeError, ValueError):
            return None
    message = ExportTraceServiceResponse()
    try:
        message.ParseFromString(body)
    except Exception:
        return None
    return int(message.partial_success.rejected_spans)


def classify(response: httpx.Response) -> ExportOutcome:
    status = response.status_code
    if status == 200:
        rejected = _rejected_spans(response)
        if rejected is None:
            return ExportOutcome("blocked", "export_rejected")
        # Sem identificar o subconjunto aceito, o lote inteiro fica retido como rejeitado.
        return ExportOutcome("rejected", "export_partial") if rejected else ExportOutcome("ok")
    if status in RETRYABLE_STATUS:
        return ExportOutcome("retry", RETRYABLE_STATUS[status], _retry_after(response))
    if status in UNAUTHORIZED_STATUS:
        return ExportOutcome("blocked", "export_unauthorized")
    return ExportOutcome("blocked", "export_rejected")


def _default_transport(request: httpx.Request, timeout: float) -> httpx.Response:
    with httpx.Client(follow_redirects=False, timeout=httpx.Timeout(timeout), trust_env=False) as (
        client
    ):
        return client.send(request)


class LangfuseSpanExporter(SpanExporter):
    """Exporter com resultado observável: `last_outcome` descreve o último lote enviado."""

    def __init__(
        self,
        destination: Destination,
        public_key: str,
        secret_key: str,
        *,
        timeout: float = EXPORT_TIMEOUT_SECONDS,
        transport: Transport = _default_transport,
    ) -> None:
        self._endpoint = destination.endpoint
        token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode("ascii")
        self._headers = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/x-protobuf",
            "x-langfuse-ingestion-version": INGESTION_VERSION,
        }
        self._timeout = timeout
        self._transport = transport
        self.last_outcome = ExportOutcome("ok")

    def send(self, descriptors: Sequence[SpanDescriptor]) -> ExportOutcome:
        return self._post(encode(descriptors))

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.last_outcome = self._post(encode_spans(spans).SerializeToString())
        return (
            SpanExportResult.SUCCESS
            if self.last_outcome.kind == "ok"
            else (SpanExportResult.FAILURE)
        )

    def _post(self, body: bytes) -> ExportOutcome:
        request = httpx.Request("POST", self._endpoint, content=body, headers=self._headers)
        try:
            response = self._transport(request, self._timeout)
        except httpx.TimeoutException:
            outcome = ExportOutcome("retry", "export_timeout")
        except httpx.HTTPError:
            outcome = ExportOutcome("retry", "export_unavailable")
        else:
            outcome = classify(response)
        self.last_outcome = outcome
        return outcome

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True
