"""Contratos do controle de execução: estado durável, reservas, pendências e reconciliação.

Este módulo não depende do histórico: os DTOs atravessam a fachada sem SQL nem conexões.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# Motivos terminais estáveis consumidos por relatório, CLI e integrações futuras.
ACTION_LIMIT = "action_limit"
DURATION_LIMIT = "duration_limit"
ATTEMPT_LIMIT = "attempt_limit"
NO_PROGRESS = "no_progress"
USER_CANCELLED = "user_cancelled"
LIMIT_REASONS = frozenset({ACTION_LIMIT, DURATION_LIMIT, ATTEMPT_LIMIT, NO_PROGRESS})
STOP_STATUSES = frozenset({"cancelled", "limit_reached", "interrupted"})

# Drenagem e encerramento compartilham um único deadline absoluto a partir do latch.
DRAIN_SECONDS = 10.0
FINALIZE_SECONDS = 15.0
WARNING_RATIO = 0.8
# Chamadas fora da contagem: consulta de aprovação e encerramento controlado.
EXEMPT_TOOLS = frozenset({"approval_status", "end_run"})

LIMIT_LABELS = {
    ACTION_LIMIT: "action limit",
    DURATION_LIMIT: "duration limit",
    ATTEMPT_LIMIT: "attempt limit",
    NO_PROGRESS: "no progress",
}


class WriteState(StrEnum):
    PREPARED = "prepared"
    SENDING = "sending"
    RESPONSE_KNOWN = "response_known"
    RESPONSE_UNKNOWN = "response_unknown"


class ReconciliationState(StrEnum):
    ALREADY_APPLIED = "already_applied"
    READY_FOR_REVIEW = "ready_for_review"
    CONFLICT = "conflict"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class ExecutionState:
    """Snapshot durável do controle da run; o primeiro motivo de parada nunca muda."""

    run_id: str
    deadline_at: str
    max_minutes: int
    max_actions: int
    max_attempts: int
    max_no_progress_actions: int
    actions_reserved: int
    actions_completed: int
    no_progress_count: int
    event_cursor: int
    duration_warned: bool
    actions_warned: bool
    stop_status: str | None
    stop_reason: str | None
    stop_requested_at: str | None
    drain_deadline_at: str | None
    finalize_deadline_at: str | None
    revision: int

    @property
    def stopped(self) -> bool:
        return self.stop_status is not None

    @property
    def admission_closed(self) -> bool:
        return self.stopped or self.actions_reserved >= self.max_actions


@dataclass(frozen=True)
class Notice:
    """Aviso entregue ao agente no próximo resultado MCP, sem chamada paga dedicada."""

    kind: str
    dimension: str | None = None
    used: int | None = None
    limit: int | None = None
    message: str | None = None
    status: str | None = None
    reason: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {key: value for key, value in self.__dict__.items() if value is not None}


def warning_notice(dimension: str, used: int, limit: int) -> Notice:
    noun = "action" if dimension == "actions" else "duration"
    return Notice(
        kind="warning",
        dimension=dimension,
        used=used,
        limit=limit,
        message=f"80% of {noun} budget used; wrap up.",
    )


@dataclass(frozen=True)
class Reservation:
    """Resultado da admissão: reservada com ordinal ou recusada sem consumir unidade."""

    admitted: bool
    action_id: str
    ordinal: int = 0
    last: bool = False
    attempt_no: int | None = None
    stop_status: str | None = None
    stop_reason: str | None = None
    warning: Notice | None = None


@dataclass(frozen=True)
class ActionOutcome:
    """Conclusão de uma ação contabilizada: progresso, tentativas e eventual latch."""

    progressed: bool
    no_progress_count: int
    attempt_no: int | None
    stop_status: str | None = None
    stop_reason: str | None = None
    latched: bool = False


@dataclass(frozen=True)
class StopLatch:
    state: ExecutionState
    won: bool


@dataclass(frozen=True)
class WriteIntentInput:
    run_id: str
    action_id: str
    binding_id: str
    operation: str
    entity: str
    record_id: str | None
    tenant: str
    detail_path: str | None
    before: dict[str, str] | None
    intended: dict[str, str] | None
    comparable: bool


@dataclass(frozen=True)
class WriteIntent:
    id: str
    run_id: str
    action_id: str
    binding_id: str
    operation: str
    entity: str
    record_id: str | None
    tenant: str
    detail_path: str | None
    before: dict[str, str] | None
    intended: dict[str, str] | None
    comparable: bool
    state: WriteState
    response_status: int | None
    created_at: str
    sent_at: str | None
    resolved_at: str | None


@dataclass(frozen=True)
class Reconciliation:
    id: str
    resume_run_id: str
    source_write_id: str
    state: ReconciliationState
    reason: str
    observation_artifact_id: str | None
    replacement_write_id: str | None
    created_at: str


@dataclass(frozen=True)
class PendingItem:
    """Pendência estruturada derivada de registros duráveis, nunca de mensagem do agente."""

    kind: str
    reference: str
    state: str
    requires_manual_review: bool
    detail: str = ""
    run_id: str | None = None

    def as_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "kind": self.kind,
            "reference": self.reference,
            "state": self.state,
            "requires_manual_review": self.requires_manual_review,
        }
        if self.detail:
            data["detail"] = self.detail
        if self.run_id is not None:
            data["run_id"] = self.run_id
        return data


@dataclass(frozen=True)
class ExecutionSummary:
    """Resumo durável de término: motivo estável, consumo e pendências."""

    run_id: str
    status: str
    reason: str | None
    actions_used: int
    max_actions: int
    max_minutes: int
    parent_run_id: str | None
    pending_items: tuple[PendingItem, ...] = field(default_factory=tuple)
    report_kind: str | None = None
    report_path: str | None = None

    def as_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "run_id": self.run_id,
            "status": self.status,
            "reason": self.reason,
            "actions_used": self.actions_used,
            "max_actions": self.max_actions,
            "pending_items": [item.as_json() for item in self.pending_items],
            "report_kind": self.report_kind,
            "report_path": self.report_path,
        }
        if self.parent_run_id is not None:
            data["parent_run_id"] = self.parent_run_id
        return data


def stop_message(reason: str | None, limit: int | None, report: str | None) -> str:
    """Linha impressa no terminal ao parar por limite ou cancelamento."""
    if reason == USER_CANCELLED:
        head = "Run cancelled."
    else:
        label = LIMIT_LABELS.get(reason or "", reason or "limit")
        head = f"Run stopped: {label}" + (f" ({limit})" if limit is not None else "") + " reached."
    return head + (f" Partial report: {report}" if report else "")
