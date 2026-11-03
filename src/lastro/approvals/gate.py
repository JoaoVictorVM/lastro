"""Gate entre a decisão `require_approval` da política e o efeito da tool.

A aprovação nunca executa uma ação armazenada: o executor repete a tool, o servidor reconstrói
a ação a partir da página real e só consome a aprovação se o fingerprint recomputado coincidir.
"""

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from lastro.approvals.models import (
    ApprovalAction,
    approve_command,
    changed_message,
    first_difference,
)
from lastro.history import (
    ApprovalError,
    ApprovalNotPendingError,
    ApprovalRecord,
    ApprovalStatus,
    HistoryStore,
)
from lastro.timeutil import parse_timestamp, utc_now

AWAITING = "awaiting_approval"
STATUS_POLL_SECONDS = 0.25
GUIDANCE = {
    ApprovalStatus.PENDING: "Still waiting for a human decision; call approval_status again.",
    ApprovalStatus.APPROVED: (
        "Retry the same tool call with the same arguments plus approval_id. Do not change them."
    ),
    ApprovalStatus.SKIPPED: "The action was skipped and not executed; continue without it.",
    ApprovalStatus.EXPIRED: (
        "No response within 15 minutes: the action was skipped and not executed."
    ),
    ApprovalStatus.ENDED: "The user ended the run at this gate; stop working on this run.",
    ApprovalStatus.INVALIDATED: "The action changed after approval; a new request is needed.",
    ApprovalStatus.CONSUMED: "The approved action was already executed once.",
    ApprovalStatus.EXECUTION_FAILED: (
        "The approved action failed after consumption; it is never replayed automatically."
    ),
}


class Notifier(Protocol):
    async def notify(self, record: ApprovalRecord) -> object: ...


@dataclass(frozen=True)
class RetryOutcome:
    kind: Literal["execute", "changed", "rejected", "pending"]
    record: ApprovalRecord | None
    reason: str = ""
    changed_field: str = ""


def describe(record: ApprovalRecord) -> dict[str, Any]:
    data: dict[str, Any] = {
        "approval_id": record.action_id,
        "status": record.status.value,
        "fingerprint": record.fingerprint,
        "summary": record.summary,
        "expires_at": record.expires_at,
        "next": GUIDANCE[record.status],
    }
    if record.decided_by is not None:
        data["decided_by"] = record.decided_by.value
        data["decided_at"] = record.decided_at
    if record.decision is not None:
        data["decision"] = record.decision.value
    if record.decision_reason is not None and record.decided_by is not None:
        data["reason"] = record.decision_reason
    return data


def awaiting_response(record: ApprovalRecord) -> dict[str, Any]:
    return {
        "decision": AWAITING,
        "approval_id": record.action_id,
        "fingerprint": record.fingerprint,
        "expires_at": record.expires_at,
        "summary": record.summary,
        "command": approve_command(record.action_id),
        "next": ("Wait with approval_status; after approve, retry the same call with approval_id."),
    }


class ApprovalGate:
    def __init__(
        self,
        history: HistoryStore,
        run_id: str,
        *,
        notifier: Notifier | None = None,
        clock: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.history, self.run_id = history, run_id
        self.notifier = notifier
        self.clock = clock
        self.sleep = sleep
        self.timers: set[asyncio.TimerHandle] = set()

    def reconcile(self) -> list[ApprovalRecord]:
        return self.history.expire_approvals(self.run_id)

    def pending(self) -> ApprovalRecord | None:
        return self.history.pending_approval(self.run_id)

    async def request(self, action: ApprovalAction, action_id: str) -> dict[str, Any]:
        """Primeira tentativa: grava o pedido e notifica; nenhum efeito no alvo acontece aqui."""
        record = self.history.request_approval(
            self.run_id, action_id, action.fingerprint, action.payload()
        )
        self.schedule(record)
        if self.notifier is not None:
            # Falha de notificação é auditada pelo notifier e nunca altera o pedido.
            with contextlib.suppress(Exception):
                await self.notifier.notify(record)
        return awaiting_response(record)

    def retry(self, approval_id: str, action: ApprovalAction, correlation_id: str) -> RetryOutcome:
        """Recompara a ação reconstruída; uma aprovação compatível é consumida antes do efeito."""
        self.reconcile()
        record = self.history.get_approval(approval_id, self.run_id)
        if record is None:
            return RetryOutcome("rejected", None, "approval_not_found")
        if record.status == ApprovalStatus.PENDING:
            return RetryOutcome("pending", record, "approval_pending")
        if record.status != ApprovalStatus.APPROVED:
            return RetryOutcome("rejected", record, "approval_" + record.status.value)
        if record.fingerprint != action.fingerprint:
            changed = first_difference(record.payload.get("components"), action.components)
            field_name = changed or "page_state"
            invalidated = self.history.invalidate_approval(self.run_id, approval_id, field_name)
            return RetryOutcome("changed", invalidated, "fingerprint_changed", field_name)
        try:
            consumed = self.history.consume_approval(
                self.run_id, approval_id, action.fingerprint, correlation_id
            )
        except ApprovalError as error:
            return RetryOutcome("rejected", record, error.code)
        return RetryOutcome("execute", consumed)

    async def renew(
        self, previous: RetryOutcome, action: ApprovalAction, action_id: str
    ) -> dict[str, Any]:
        """Pedido novo depois de invalidar o anterior; o id novo só existe após persistir."""
        response = await self.request(action, action_id)
        response["message"] = changed_message(previous.changed_field)
        response["changed_field"] = previous.changed_field
        if previous.record is not None:
            response["previous_approval_id"] = previous.record.action_id
        return response

    def execution_failed(self, approval_id: str, error_code: str) -> None:
        with contextlib.suppress(ApprovalError):
            self.history.fail_approval_execution(self.run_id, approval_id, error_code)

    def skip_pending(self, reason: str) -> None:
        """Encerramento pelo agente: o pedido aberto vira skip do sistema, nunca aprovação."""
        record = self.pending()
        if record is None:
            return
        with contextlib.suppress(ApprovalNotPendingError):
            self.history.decide_approval(
                record.action_id, "skip", "system", note=reason, run_id=self.run_id
            )

    async def status(self, approval_id: str, wait_seconds: int) -> ApprovalRecord | None:
        """Consulta com espera curta; a tool apenas observa, nunca decide nem executa."""
        deadline = time.monotonic() + wait_seconds
        while True:
            self.reconcile()
            record = self.history.get_approval(approval_id, self.run_id)
            if record is None or record.status != ApprovalStatus.PENDING:
                return record
            if time.monotonic() >= deadline:
                return record
            await self.sleep(STATUS_POLL_SECONDS)

    def schedule(self, record: ApprovalRecord) -> None:
        """Timer de expiração; leituras e startup também reconciliam, então crash não estende."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        delay = (parse_timestamp(record.expires_at) - self.clock()).total_seconds()

        def fire() -> None:
            self.timers.discard(handle)
            with contextlib.suppress(Exception):
                self.reconcile()

        handle = loop.call_later(max(delay, 0.0) + 0.05, fire)
        self.timers.add(handle)

    def close(self) -> None:
        for handle in self.timers:
            handle.cancel()
        self.timers.clear()
