"""Contratos do modo autônomo: triggers, fila, agenda, comparação e notificações.

Nada aqui faz rede, processo ou SQL. Fingerprints usam JSON canônico e SHA-256, para que o mesmo
conteúdo produza sempre a mesma chave idempotente.
"""

import hashlib
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, cast

QUEUE_CAPACITY = 3
LEASE_SECONDS = 90
HEARTBEAT_SECONDS = 20
DEFAULT_MAX_MINUTES = 30
NATIVE_CAP_NUSD = 2_000_000_000
LATE_WINDOW_MINUTES = 60
# Instantes alvo das três tentativas totais de cada canal, em segundos desde a criação.
NOTIFICATION_SCHEDULE = (0, 60, 300)
MAX_NOTIFICATION_ATTEMPTS = len(NOTIFICATION_SCHEDULE)
MAX_SUMMARY_LINES = 20
MAX_TRIGGER_KEY = 200
MAX_MISSION_CHARS = 10_000
NO_BASELINE_MESSAGE = "No previous run to compare; all findings shown as new."

USAGE_EXIT_CODE = 2
OPERATIONAL_EXIT_CODE = 1
INTERRUPTED_EXIT_CODE = 130


class AutonomousError(Exception):
    """Erro tipado do modo autônomo; `code` é estável na CLI, no JSON e nos eventos."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{message} ({code})")
        self.code = code
        self.message = message


INVALID_INPUT = "invalid_autonomous_input"
SCHEDULE_CONFLICT = "schedule_conflict"
SCHEDULER_UNAVAILABLE = "scheduler_unavailable"
TRIGGER_CONFLICT = "trigger_conflict"
QUEUE_UNAVAILABLE = "queue_unavailable"
LEASE_BUSY = "lease_busy"
LEASE_LOST = "lease_lost"
MISSION_SNAPSHOT_INVALID = "mission_snapshot_invalid"
PREFLIGHT_FAILED = "autonomous_preflight_failed"
COMPARISON_UNAVAILABLE = "comparison_unavailable"
NOTIFICATION_FAILED = "notification_failed"

USAGE_CODES = frozenset(
    {INVALID_INPUT, SCHEDULE_CONFLICT, TRIGGER_CONFLICT, MISSION_SNAPSHOT_INVALID}
)


class TriggerKind(StrEnum):
    SCHEDULE = "schedule"
    MANUAL = "manual"
    WORKFLOW_RUN = "workflow_run"
    DEPLOYMENT_STATUS = "deployment_status"

    @property
    def post_deploy(self) -> bool:
        return self in {TriggerKind.WORKFLOW_RUN, TriggerKind.DEPLOYMENT_STATUS}


class QueueStatus(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    DROPPED = "dropped"

    @property
    def active(self) -> bool:
        return self in {QueueStatus.CLAIMED, QueueStatus.RUNNING}

    @property
    def terminal(self) -> bool:
        return self in {
            QueueStatus.COMPLETED,
            QueueStatus.BLOCKED,
            QueueStatus.FAILED,
            QueueStatus.DROPPED,
        }


class ScheduleState(StrEnum):
    ACTIVE = "active"
    DRIFTED = "drifted"
    DISABLED = "disabled"
    REMOVED = "removed"


class InstallState(StrEnum):
    """Reconciliação com o agendador do SO, calculada em leitura."""

    INSTALLED = "installed"
    DRIFTED = "drifted"
    DISABLED = "disabled"
    MISSING = "missing"


class OccurrenceStatus(StrEnum):
    QUEUED = "queued"
    DUPLICATE = "duplicate"
    MISSED = "missed"
    NOT_DUE = "not_due"


class ComparisonStatus(StrEnum):
    COMPARED = "compared"
    NO_BASELINE = "no_baseline"
    NOT_COMPARABLE = "not_comparable"
    FAILED = "failed"


class MatchLabel(StrEnum):
    NEW = "new"
    RECURRING = "recurring"
    RESOLVED = "resolved"
    REGRESSED = "regressed"
    NOT_COMPARABLE = "not_comparable"


COUNTED_LABELS = (MatchLabel.NEW, MatchLabel.REGRESSED, MatchLabel.RECURRING, MatchLabel.RESOLVED)


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "notification_failed"


class AttemptStatus(StrEnum):
    DELIVERED = "delivered"
    RETRYABLE = "retryable"
    PERMANENT = "permanent_failure"


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(16)}"


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_of(value: object) -> str:
    data = value if isinstance(value, str) else canonical(value)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MissionSnapshot:
    """Missão congelada no instante do trigger; a execução nunca relê outra versão."""

    kind: str
    ref: str | None
    text: str

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise AutonomousError(MISSION_SNAPSHOT_INVALID, "mission text is empty")
        if len(self.text) > MAX_MISSION_CHARS:
            raise AutonomousError(
                MISSION_SNAPSHOT_INVALID, f"mission exceeds {MAX_MISSION_CHARS} characters"
            )

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def as_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "ref": self.ref, "text": self.text, "sha256": self.sha256}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "MissionSnapshot":
        snapshot = cls(str(data["kind"]), data.get("ref"), str(data["text"]))
        if data.get("sha256") not in (None, snapshot.sha256):
            raise AutonomousError(MISSION_SNAPSHOT_INVALID, "frozen mission hash mismatch")
        return snapshot


@dataclass(frozen=True)
class TriggerInput:
    """Trigger já validado; `payload` contém somente campos em allowlist."""

    kind: TriggerKind
    key: str
    profile: str
    mission: MissionSnapshot
    payload: Mapping[str, Any] = field(default_factory=dict[str, Any])
    schedule_id: str | None = None
    executor: str | None = None

    def __post_init__(self) -> None:
        if not 1 <= len(self.key) <= MAX_TRIGGER_KEY:
            raise AutonomousError(INVALID_INPUT, "trigger key must have 1 to 200 characters")
        if self.kind == TriggerKind.SCHEDULE and self.schedule_id is None:
            raise AutonomousError(INVALID_INPUT, "schedule trigger requires its schedule")

    def snapshot(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "key": self.key,
            "profile": self.profile,
            "schedule_id": self.schedule_id,
            "executor": self.executor,
            "payload": dict(self.payload),
        }

    @property
    def fingerprint(self) -> str:
        """Corpo canônico: mesma chave com fingerprint diferente é conflito."""
        return sha256_of({"trigger": self.snapshot(), "mission_sha256": self.mission.sha256})


@dataclass(frozen=True)
class QueueItem:
    id: str
    project_id: str
    profile: str
    schedule_id: str | None
    trigger_kind: TriggerKind
    trigger_key: str
    trigger_sha256: str
    mission: MissionSnapshot
    change_context_ref: str | None
    trigger_snapshot: Mapping[str, Any]
    status: QueueStatus
    reason: str | None
    queued_at: str
    claimed_at: str | None
    started_at: str | None
    ended_at: str | None
    run_id: str | None
    report_snapshot_id: str | None

    @property
    def executor(self) -> str | None:
        value = self.trigger_snapshot.get("executor")
        return value if isinstance(value, str) else None

    def public_json(self) -> dict[str, Any]:
        """Sem texto da missão: somente referência, hash e metadados."""
        return {
            "item_id": self.id,
            "profile": self.profile,
            "trigger": self.trigger_kind.value,
            "trigger_key": self.trigger_key,
            "status": self.status.value,
            "reason": self.reason,
            "mission_ref": self.mission.ref,
            "mission_sha256": self.mission.sha256,
            "queued_at": self.queued_at,
            "run_id": self.run_id,
        }


@dataclass(frozen=True)
class EnqueueResult:
    item: QueueItem
    created: bool
    dropped: tuple[QueueItem, ...] = ()
    position: int | None = None


@dataclass(frozen=True)
class Lease:
    project_id: str
    profile: str
    token: str
    generation: int
    expires_at: str


@dataclass(frozen=True)
class ScheduleDefinition:
    id: str
    project_id: str
    name: str
    profile: str
    mission_path: str
    daily_time: str
    timezone: str
    executor: str
    settings: Mapping[str, Any]
    fingerprint: str
    backend: str
    external_id: str | None
    state: ScheduleState
    created_at: str
    updated_at: str
    removed_at: str | None


@dataclass(frozen=True)
class Occurrence:
    id: str
    schedule_id: str
    local_date: str
    due_at: str
    observed_at: str
    status: OccurrenceStatus
    item_id: str | None
    reason: str | None


@dataclass(frozen=True)
class MatchRecord:
    flow: str
    label: MatchLabel
    current_finding_id: str | None
    previous_finding_id: str | None
    signature_sha256: str
    score: float | None
    match_rule: str
    detail: Mapping[str, Any] = field(default_factory=dict[str, Any])

    def as_json(self) -> dict[str, Any]:
        return {
            "label": self.label.value,
            "flow": self.flow,
            "finding_id": self.current_finding_id,
            "previous_finding_id": self.previous_finding_id,
            "score": self.score,
            "match_rule": self.match_rule,
        }


@dataclass(frozen=True)
class ComparisonResult:
    status: ComparisonStatus
    run_id: str
    baseline_run_id: str | None
    algorithm: str
    algorithm_sha256: str
    coverage: Mapping[str, str]
    matches: tuple[MatchRecord, ...]
    limitations: tuple[str, ...]

    @property
    def counts(self) -> dict[str, int]:
        return {
            label.value: sum(1 for item in self.matches if item.label == label)
            for label in COUNTED_LABELS
        }

    def as_json(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "run_id": self.run_id,
            "baseline_run_id": self.baseline_run_id,
            "algorithm": self.algorithm,
            "coverage": dict(self.coverage),
            "counts": self.counts,
            "items": [item.as_json() for item in self.matches],
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True)
class StoredComparison:
    id: str
    queue_item_id: str
    result: ComparisonResult
    created_at: str


@dataclass(frozen=True)
class NotificationMessage:
    """Projeção canônica, redigida e sem evidência; o mesmo conteúdo vai a todos os canais."""

    profile: str
    status: str
    termination_reason: str | None
    counts: Mapping[str, int]
    pending_approvals: int
    clarification_gaps: int
    other_pending: int
    report: str | None
    comparison: str

    @property
    def title(self) -> str:
        return f"Lastro · {self.profile}"

    def as_json(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "profile": self.profile,
            "status": self.status,
            "summary": {
                label.value: int(self.counts.get(label.value, 0)) for label in COUNTED_LABELS
            },
            "termination_reason": self.termination_reason,
            "pending_approvals": self.pending_approvals,
            "clarification_gaps": self.clarification_gaps,
            "other_pending": self.other_pending,
            "report": self.report,
            "comparison": self.comparison,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "NotificationMessage":
        summary = cast(dict[str, Any], data.get("summary") or {})
        counts = {str(key): int(value) for key, value in summary.items()}
        return cls(
            profile=str(data["profile"]),
            status=str(data["status"]),
            termination_reason=data.get("termination_reason"),
            counts=counts,
            pending_approvals=int(data.get("pending_approvals", 0)),
            clarification_gaps=int(data.get("clarification_gaps", 0)),
            other_pending=int(data.get("other_pending", 0)),
            report=data.get("report"),
            comparison=str(data.get("comparison", "")),
        )

    @property
    def sha256(self) -> str:
        return sha256_of(self.as_json())

    def lines(self) -> tuple[str, ...]:
        """Texto humano com no máximo 20 linhas, aplicado antes de qualquer adapter."""
        counts = {key: int(value) for key, value in self.counts.items()}
        headline = (
            f"{self.title} · {counts.get('new', 0)} new findings, "
            f"{counts.get('regressed', 0)} regressed, {counts.get('recurring', 0)} recurring"
        )
        reason = f" ({self.termination_reason})" if self.termination_reason else ""
        lines = [
            headline,
            f"status: {self.status}{reason}",
            f"resolved since previous run: {counts.get('resolved', 0)}",
        ]
        if self.comparison:
            lines.append(f"comparison: {self.comparison}")
        if self.pending_approvals:
            noun = "action" if self.pending_approvals == 1 else "actions"
            lines.append(f"{self.pending_approvals} {noun} skipped awaiting approval")
        if self.clarification_gaps:
            lines.append(f"{self.clarification_gaps} clarification gap(s) left unanswered")
        if self.other_pending:
            lines.append(f"{self.other_pending} other pending item(s)")
        lines.append(f"report: {self.report or 'not available'}")
        return tuple(line[:300] for line in lines[:MAX_SUMMARY_LINES])

    @property
    def text(self) -> str:
        return "\n".join(self.lines())


@dataclass(frozen=True)
class NotificationRecord:
    id: str
    queue_item_id: str
    channel_name: str
    channel_type: str
    message: NotificationMessage
    message_sha256: str
    destination_label: str
    status: DeliveryStatus
    attempts: int
    next_attempt_at: str | None
    delivered_at: str | None
    last_error_code: str | None
    created_at: str


@dataclass(frozen=True)
class DeliveryResult:
    """Resultado de uma tentativa; nunca carrega corpo de resposta nem endereço secreto."""

    status: AttemptStatus
    response_code: int | None = None
    error_code: str | None = None
    latency_ms: int | None = None
