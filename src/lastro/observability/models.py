"""Contratos sem dependência do SDK: descritor sanitizado, resultado de envio e estado local."""

import json
from dataclasses import dataclass, field
from typing import Literal, cast

type AttributeValue = str | int | float | bool | list[str]
type Attributes = dict[str, AttributeValue]
type ExportKind = Literal["ok", "retry", "blocked", "rejected"]
type ExportStatus = Literal["pending", "idle", "suspended", "blocked"]

MAX_ATTRIBUTES = 64
MAX_STRING_LENGTH = 512
MAX_ARRAY_ITEMS = 16
MAX_DESCRIPTOR_BYTES = 32 * 1024


@dataclass(frozen=True)
class SpanDescriptor:
    """Span lógico de um evento durável; os timestamps são os do histórico (UTC, F03)."""

    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    start_time: str
    end_time: str
    attributes: Attributes
    resource: dict[str, str] = field(default_factory=dict[str, str])

    def as_json(self) -> dict[str, object]:
        return {
            "attributes": dict(self.attributes),
            "end_time": self.end_time,
            "name": self.name,
            "parent_span_id": self.parent_span_id,
            "resource": dict(self.resource),
            "span_id": self.span_id,
            "start_time": self.start_time,
            "trace_id": self.trace_id,
        }

    def canonical(self) -> str:
        return json.dumps(self.as_json(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "SpanDescriptor":
        data = cast(dict[str, object], json.loads(text))
        parent = data.get("parent_span_id")
        return cls(
            trace_id=str(data["trace_id"]),
            span_id=str(data["span_id"]),
            parent_span_id=str(parent) if parent is not None else None,
            name=str(data["name"]),
            start_time=str(data["start_time"]),
            end_time=str(data["end_time"]),
            attributes=cast(Attributes, data["attributes"]),
            resource=cast(dict[str, str], data.get("resource") or {}),
        )


@dataclass(frozen=True)
class ExportOutcome:
    """Resultado observado na fronteira HTTP; nunca carrega corpo, headers nem exceção."""

    kind: ExportKind
    code: str | None = None
    retry_after_seconds: float | None = None


@dataclass(frozen=True)
class ObservabilityStatus:
    run_id: str
    trace_id: str
    trace_url: str
    export_status: ExportStatus
    projected_events: int
    acknowledged_spans: int
    pending_spans: int
    waiting_end: int
    rejected_spans: int
    last_error_code: str | None
    suite_id: str | None
    condition: str | None

    def as_json(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "export_status": self.export_status,
            "projected_events": self.projected_events,
            "acknowledged_spans": self.acknowledged_spans,
            "pending_spans": self.pending_spans,
            "rejected_spans": self.rejected_spans,
            "last_error_code": self.last_error_code,
        }
