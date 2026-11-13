"""Projeção determinística de eventos duráveis em descritores de span.

Um evento gera exatamente um span, filho plano da raiz (`run_started`). Somente atributos de
uma lista fechada saem daqui, sempre depois da redação central e de validação por enum, regex
ou inteiro. Nada de missão, argumentos, reason livre, URLs do alvo, prompts ou respostas.
"""

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import cast

from lastro.config.observability import MAPPING_VERSION
from lastro.executors import EXECUTORS
from lastro.history.database import db_timestamp
from lastro.history.models import EventRecord, EventType, JsonValue, PolicyDecision
from lastro.history.native import ModelCall
from lastro.history.redaction import redact, redact_text
from lastro.observability.models import (
    MAX_ARRAY_ITEMS,
    MAX_ATTRIBUTES,
    MAX_DESCRIPTOR_BYTES,
    MAX_STRING_LENGTH,
    Attributes,
    AttributeValue,
    SpanDescriptor,
)

TRACE_NAMESPACE = "lastro/trace/v1"
SPAN_NAMESPACE = "lastro/span/v1"
TRACE_NAME = "Lastro run"
UNKNOWN_TOOL = "unknown"
EXTERNAL_MODEL_REASON = "executor does not expose it"

STAGES = (
    "mission_preparation",
    "memory_lookup",
    "observation",
    "action",
    "policy_evaluation",
    "approval",
    "verification",
    "reproduction",
    "consolidation",
)
SUPPORT_STAGES = ("model_call", "lifecycle", "knowledge")

KNOWN_TOOLS = frozenset(
    {
        "mission_context",
        "submit_mission_plan",
        "record_test_check",
        "observe",
        "navigate",
        "click",
        "fill",
        "select",
        "submit",
        "screenshot",
        "wait_for",
        "report_finding",
        "record_criterion_result",
        "start_evidence_video",
        "stop_evidence_video",
        "ask_clarification",
        "approval_status",
        "end_run",
        "generate_test_data",
        "knowledge_query",
        "knowledge_propose",
        "knowledge_revalidate",
        "report_reproduction_result",
    }
)
_TOOL_STAGES: dict[str, str] = {
    "mission_context": "mission_preparation",
    "submit_mission_plan": "mission_preparation",
    "ask_clarification": "mission_preparation",
    "knowledge_query": "memory_lookup",
    "observe": "observation",
    "screenshot": "observation",
    "wait_for": "observation",
    "start_evidence_video": "observation",
    "stop_evidence_video": "observation",
    "approval_status": "approval",
    "record_test_check": "verification",
    "record_criterion_result": "verification",
    "knowledge_revalidate": "verification",
    "report_finding": "verification",
    "report_reproduction_result": "reproduction",
    "knowledge_propose": "knowledge",
    "end_run": "lifecycle",
}
_EVENT_STAGES: dict[EventType, str] = {
    EventType.RUN_STARTED: "mission_preparation",
    EventType.NATIVE_STARTED: "mission_preparation",
    EventType.CLARIFICATION_REQUESTED: "mission_preparation",
    EventType.CLARIFICATION_ANSWERED: "mission_preparation",
    EventType.POLICY_DECISION: "policy_evaluation",
    EventType.SECURITY_EVENT: "policy_evaluation",
    EventType.APPROVAL_REQUESTED: "approval",
    EventType.APPROVAL_DECIDED: "approval",
    EventType.AUTHENTICATION: "verification",
    EventType.ORACLE_SIGNAL: "verification",
    EventType.FINDING_SUBMITTED: "verification",
    EventType.RECORD_CREATED: "action",
    EventType.WRITE_RECONCILED: "action",
    EventType.REPRODUCTION_STARTED: "reproduction",
    EventType.REPRODUCTION_ATTEMPT_FINISHED: "reproduction",
    EventType.REPRODUCTION_VERDICT: "reproduction",
    EventType.MODEL_CALL_STARTED: "model_call",
    EventType.MODEL_CALL_COMPLETED: "model_call",
    EventType.MODEL_CALL_FAILED: "model_call",
}
_COMPLETED_EVENTS = frozenset(
    {
        EventType.RECORD_CREATED,
        EventType.WRITE_RECONCILED,
        EventType.MODEL_CALL_COMPLETED,
        EventType.REPRODUCTION_ATTEMPT_FINISHED,
    }
)
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_RUN_ID = re.compile(r"^run_\d{8}_\d{6}_[0-9a-f]{1,32}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_MODEL = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_DECIMAL = re.compile(r"^\d{1,12}(\.\d{1,9})?$")
_SUBTYPE_KEYS = ("subtype", "code")
_ENUM_KEYS: dict[EventType, tuple[tuple[str, str], ...]] = {
    EventType.APPROVAL_DECIDED: (("status", "lastro.approval.status"),),
    EventType.REPRODUCTION_ATTEMPT_FINISHED: (
        ("outcome", "lastro.reproduction.outcome"),
        ("reason", "lastro.reproduction.reason"),
        ("executor", "lastro.reproduction.executor"),
    ),
    EventType.REPRODUCTION_VERDICT: (
        ("classification", "lastro.reproduction.classification"),
        ("reason", "lastro.reproduction.reason"),
    ),
    EventType.REPRODUCTION_STARTED: (
        ("mode", "lastro.reproduction.mode"),
        ("explorer_executor", "lastro.reproduction.explorer"),
        ("verifier_executor", "lastro.reproduction.executor"),
    ),
    EventType.RUN_ENDED: (("status", "lastro.run.status"),),
    EventType.LIMIT_HIT: (("limit", "lastro.limit.kind"),),
    EventType.KNOWLEDGE_PROPOSED: (("outcome", "lastro.memory.outcome"),),
}


class ProjectionError(Exception):
    code = "obs_projection_failed"


def _first_nonzero(digest: bytes, width: int) -> str:
    for start in range(0, len(digest) - width + 1, width):
        chunk = digest[start : start + width]
        if any(chunk):
            return chunk.hex()
    return (1).to_bytes(width, "big").hex()


def _digest(parts: list[object]) -> bytes:
    canonical = json.dumps(parts, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).digest()


def trace_id_for(project_id: str, run_id: str) -> str:
    return _first_nonzero(_digest([TRACE_NAMESPACE, project_id, run_id]), 16)


def span_id_for(project_id: str, run_id: str, event_id: int) -> str:
    return _first_nonzero(_digest([SPAN_NAMESPACE, project_id, run_id, event_id]), 8)


@dataclass(frozen=True)
class RunProjection:
    """Leitura consistente de uma run para a projeção; montada na transação do histórico."""

    project_id: str
    run_id: str
    executor: str
    started_at: str
    ended_at: str | None
    lastro_version: str
    root_event_id: int
    origin_run_id: str | None = None
    suite_id: str | None = None
    condition: str | None = None
    memory_outcome: str | None = None
    model_calls: tuple[ModelCall, ...] = ()
    decisions: Mapping[str, PolicyDecision] = field(default_factory=dict[str, PolicyDecision])
    approval_requests: Mapping[str, str] = field(default_factory=dict[str, str])
    reproduction_starts: Mapping[str, str] = field(default_factory=dict[str, str])

    @property
    def trace_id(self) -> str:
        return trace_id_for(self.project_id, self.run_id)

    @property
    def root_span_id(self) -> str:
        return span_id_for(self.project_id, self.run_id, self.root_event_id)

    def call_for_completion(self, event_id: int) -> ModelCall | None:
        matches = [
            call
            for call in self.model_calls
            if call.completion_event_id == event_id and call.run_id == self.run_id
        ]
        return matches[0] if len(matches) == 1 else None

    def call_by_id(self, call_id: object) -> ModelCall | None:
        if not isinstance(call_id, str):
            return None
        for call in self.model_calls:
            if call.id == call_id and call.run_id == self.run_id:
                return call
        return None


def tool_label(name: str | None) -> str | None:
    if name is None:
        return None
    return name if name in KNOWN_TOOLS else UNKNOWN_TOOL


def _payload(event: EventRecord) -> dict[str, JsonValue]:
    redacted = redact(event.payload)
    return cast(dict[str, JsonValue], redacted) if isinstance(redacted, dict) else {}


def stage_of(event: EventRecord, memory_outcome: str | None = None) -> tuple[str, list[str]]:
    """Estágio primário e categorias adicionais; tipo especializado vence o nome da tool."""
    kind = event.event_type
    if kind in _EVENT_STAGES:
        return _EVENT_STAGES[kind], []
    if kind == EventType.TOOL_CALL:
        tool = tool_label(event.tool_name)
        if tool is None or tool == UNKNOWN_TOOL:
            return "action", []
        return _TOOL_STAGES.get(tool, "action"), []
    if kind == EventType.KNOWLEDGE_PROPOSED:
        subtype = _payload(event).get("subtype")
        return ("consolidation", []) if subtype == "consolidation" else ("knowledge", [])
    if kind == EventType.RUN_ENDED and memory_outcome == "nothing_new":
        return "consolidation", ["lifecycle"]
    return "lifecycle", []


def operation_status(event: EventRecord, run: RunProjection) -> str | None:
    kind = event.event_type
    if kind in _COMPLETED_EVENTS:
        return "completed"
    if kind == EventType.MODEL_CALL_FAILED:
        return "failed"
    if kind not in (EventType.TOOL_CALL, EventType.POLICY_DECISION):
        return None
    decision = event.policy_decision
    if decision is None and event.correlation_id is not None:
        decision = run.decisions.get(event.correlation_id)
    if decision is None:
        return "unknown"
    # Só há prova de tentativa quando a política permitiu; negação e gate não executam.
    return "attempted" if decision == PolicyDecision.ALLOW else "not_executed"


def _parse(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def _format(moment: datetime) -> str:
    return db_timestamp(moment)


def _interval(start: str, end: str) -> tuple[str, str, int, bool]:
    begin, finish = _parse(start), _parse(end)
    if finish < begin:
        return end, end, 0, True
    return start, end, int((finish - begin).total_seconds() * 1000), False


def _code(value: JsonValue) -> str | None:
    return value if isinstance(value, str) and _CODE.match(value) else None


def _identifier(value: object) -> str | None:
    if not isinstance(value, str) or not _IDENTIFIER.match(value):
        return None
    return value if redact_text(value) == value else None


def _common(run: RunProjection, event: EventRecord, stage: str) -> Attributes:
    attrs: Attributes = {
        "lastro.run.id": run.run_id,
        "lastro.event.id": event.id,
        "lastro.event.sequence": event.sequence,
        "lastro.event.type": event.event_type.value,
        "lastro.stage": stage,
        "lastro.executor": run.executor,
        "lastro.mapping.version": MAPPING_VERSION,
        "langfuse.trace.name": TRACE_NAME,
        "langfuse.trace.metadata.run_id": run.run_id,
    }
    if run.suite_id is not None and run.condition is not None:
        attrs["langfuse.trace.metadata.suite_id"] = run.suite_id
        attrs["langfuse.trace.metadata.condition"] = run.condition
    if run.origin_run_id is not None and _RUN_ID.match(run.origin_run_id):
        attrs["lastro.origin.run_id"] = run.origin_run_id
    if run.executor != "native":
        attrs["lastro.model_data.status"] = "unavailable"
        attrs["lastro.model_data.reason"] = EXTERNAL_MODEL_REASON
    return attrs


def _model_attributes(attrs: Attributes, call: ModelCall | None, *, terminal: bool) -> None:
    if call is None:
        attrs["lastro.model_data.status"] = "unavailable"
        attrs["lastro.model_data.reason"] = "ledger entry not found"
        return
    if _MODEL.match(call.model):
        attrs["lastro.model.name"] = call.model
    if _CODE.match(call.node):
        attrs["lastro.model.node"] = call.node
    attrs["lastro.model.step_index"] = call.step_index
    attrs["lastro.model.attempt"] = call.attempt
    from lastro.native.pricing import format_usd
    from lastro.native.telemetry import from_call

    telemetry = from_call(call)
    attrs["lastro.model.reserved_cost_usd"] = format_usd(call.reserved_nusd)
    if not terminal:
        if call.completion_event_id is None:
            attrs["lastro.model.usage_status"] = "pending"
        return
    attrs["lastro.model.usage_status"] = telemetry.usage_status
    if call.error_code is not None and (code := _code(call.error_code)) is not None:
        attrs["lastro.model.error_code"] = code
    if telemetry.usage_status == "reported":
        usage = {
            "input": telemetry.input_tokens,
            "input_cache_read": telemetry.cache_read_input_tokens,
            "input_cache_write": telemetry.cache_creation_input_tokens,
            "output": telemetry.output_tokens,
        }
        counts = {name: value for name, value in usage.items() if isinstance(value, int)}
        if len(counts) != len(usage) or any(value < 0 for value in counts.values()):
            attrs["lastro.model_data.status"] = "partial"
            return
        attrs["lastro.model_data.status"] = "available"
        attrs["langfuse.observation.type"] = "generation"
        if "lastro.model.name" in attrs:
            attrs["langfuse.observation.model.name"] = attrs["lastro.model.name"]
        attrs["langfuse.observation.usage_details"] = json.dumps(counts, sort_keys=True)
        if telemetry.total_input_tokens is not None:
            attrs["lastro.model.total_input"] = telemetry.total_input_tokens
        cost = telemetry.cost_usd
        if cost is not None and _DECIMAL.match(cost):
            attrs["lastro.model.cost_usd"] = cost
            attrs["langfuse.observation.cost_details"] = json.dumps({"total": float(cost)})
        return
    if telemetry.usage_status == "not_charged":
        # Liberação do ledger é a prova de não cobrança; tokens não são inventados.
        attrs["lastro.model_data.status"] = "partial"
        attrs["lastro.model.cost_usd"] = "0"
        return
    attrs["lastro.model_data.status"] = "partial"


def _payload_attributes(
    attrs: Attributes, event: EventRecord, payload: dict[str, JsonValue]
) -> None:
    for key in _SUBTYPE_KEYS:
        if (code := _code(payload.get(key))) is not None:
            attrs["lastro.event.code"] = code
            break
    for key, name in _ENUM_KEYS.get(event.event_type, ()):
        value = payload.get(key)
        if (code := _code(value)) is not None:
            attrs[name] = code
        elif isinstance(value, str) and value in EXECUTORS:
            attrs[name] = value
    for key, name in (
        ("action_id", "lastro.action.id"),
        ("finding_id", "lastro.finding.id"),
        ("round_id", "lastro.reproduction.round_id"),
    ):
        if (identifier := _identifier(payload.get(key))) is not None:
            attrs[name] = identifier
    child = payload.get("child_run_id")
    if isinstance(child, str) and _RUN_ID.match(child):
        attrs["lastro.reproduction.child_run_id"] = child
    parent = payload.get("parent_run_id")
    if (
        event.event_type == EventType.RUN_RESUMED
        and isinstance(parent, str)
        and _RUN_ID.match(parent)
    ):
        attrs["lastro.origin.run_id"] = parent


def _enforce_limits(attrs: Attributes) -> Attributes:
    essential = {
        "lastro.run.id",
        "lastro.event.id",
        "lastro.event.sequence",
        "lastro.event.type",
        "lastro.stage",
        "lastro.mapping.version",
    }
    result: Attributes = {}
    for key, value in attrs.items():
        if isinstance(value, str) and len(value) > MAX_STRING_LENGTH:
            if key in essential:
                raise ProjectionError(f"essential attribute {key} exceeds the limit")
            continue
        if isinstance(value, list) and len(value) > MAX_ARRAY_ITEMS:
            value = value[:MAX_ARRAY_ITEMS]
        result[key] = value
    if len(result) > MAX_ATTRIBUTES:
        keep = [key for key in result if key in essential]
        rest = [key for key in result if key not in essential]
        result = {key: result[key] for key in [*keep, *rest][:MAX_ATTRIBUTES]}
    return result


def _tags(attrs: Attributes) -> list[str]:
    tags: list[str] = []
    for key in ("lastro.executor", "lastro.stage", "lastro.policy.decision"):
        value = attrs.get(key)
        if isinstance(value, str) and value not in tags:
            tags.append(value)
    return tags


def describe(event: EventRecord, run: RunProjection) -> SpanDescriptor:
    """Descritor imutável do evento; a raiz exige a run terminal (intervalo completo)."""
    if event.run_id != run.run_id:
        raise ProjectionError("event belongs to another run")
    root = event.event_type == EventType.RUN_STARTED
    if root and run.ended_at is None:
        raise ProjectionError("root span requires a terminal run")
    stage, categories = stage_of(event, run.memory_outcome)
    attrs = _common(run, event, stage)
    if categories:
        attrs["lastro.stage.categories"] = categories
    payload = _payload(event)
    tool = tool_label(event.tool_name)
    if tool is not None:
        attrs["lastro.tool.name"] = tool
    decision = event.policy_decision
    if decision is None and event.correlation_id is not None:
        decision = run.decisions.get(event.correlation_id)
    if decision is not None and event.event_type in (
        EventType.TOOL_CALL,
        EventType.POLICY_DECISION,
    ):
        attrs["lastro.policy.decision"] = decision.value
    if (status := operation_status(event, run)) is not None:
        attrs["lastro.operation.status"] = status
    if event.arguments_sha256 is not None and _HEX64.match(event.arguments_sha256):
        attrs["lastro.arguments.sha256"] = event.arguments_sha256
    if (action := _identifier(event.correlation_id)) is not None:
        attrs["lastro.action.id"] = action
    _payload_attributes(attrs, event, payload)

    start, end = event.timestamp, event.timestamp
    duration: int | None = None
    duration_status = "unavailable"
    skew = False
    if root:
        assert run.ended_at is not None
        start, end, duration, skew = _interval(run.started_at, run.ended_at)
        duration_status = "measured"
    elif event.event_type in (EventType.MODEL_CALL_COMPLETED, EventType.MODEL_CALL_FAILED):
        call = run.call_for_completion(event.id)
        _model_attributes(attrs, call, terminal=True)
        if call is not None and call.latency_ms is not None and call.latency_ms >= 0:
            begin = _parse(event.timestamp) - timedelta(milliseconds=call.latency_ms)
            start, duration, duration_status = _format(begin), call.latency_ms, "measured"
    elif event.event_type == EventType.MODEL_CALL_STARTED:
        _model_attributes(attrs, run.call_by_id(payload.get("call_id")), terminal=False)
    elif event.event_type == EventType.APPROVAL_DECIDED:
        requested = run.approval_requests.get(str(payload.get("action_id")))
        if requested is not None:
            start, end, duration, skew = _interval(requested, event.timestamp)
            duration_status = "derived"
    elif event.event_type == EventType.REPRODUCTION_VERDICT:
        began = run.reproduction_starts.get(str(payload.get("finding_id")))
        if began is not None:
            start, end, duration, skew = _interval(began, event.timestamp)
            duration_status = "derived"
    if skew:
        attrs["lastro.duration.diagnostic"] = "clock_skew"
    attrs["lastro.duration.status"] = duration_status
    if duration is not None and duration_status != "unavailable":
        attrs["lastro.duration.ms"] = duration
    attrs["langfuse.trace.tags"] = _tags(attrs)

    name_parts = [stage, event.event_type.value]
    if tool is not None:
        name_parts.append(tool)
    span_id = span_id_for(run.project_id, run.run_id, event.id)
    descriptor = SpanDescriptor(
        trace_id=run.trace_id,
        span_id=span_id,
        parent_span_id=None if root else run.root_span_id,
        name=".".join(name_parts),
        start_time=start,
        end_time=end,
        attributes=_enforce_limits(attrs),
        resource={"service.name": "lastro", "service.version": run.lastro_version},
    )
    if len(descriptor.canonical().encode("utf-8")) > MAX_DESCRIPTOR_BYTES:
        raise ProjectionError("descriptor exceeds the size limit")
    return descriptor


def describe_all(events: Iterable[EventRecord], run: RunProjection) -> list[SpanDescriptor]:
    return [describe(event, run) for event in events]


def attribute_values(descriptor: SpanDescriptor) -> list[AttributeValue]:
    return list(descriptor.attributes.values())
