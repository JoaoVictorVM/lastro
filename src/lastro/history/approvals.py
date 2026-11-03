"""SQL dos gates de aprovação; cada mudança de estado grava row, evento e transição juntos."""

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import cast

from lastro.history.database import db_timestamp
from lastro.history.errors import ApprovalError, ApprovalNotPendingError
from lastro.history.events import insert_event, prepare_event
from lastro.history.models import (
    APPROVAL_TTL_SECONDS,
    MAX_APPROVAL_NOTE_LENGTH,
    NO_RESPONSE_REASON,
    ApprovalChoice,
    ApprovalRecord,
    ApprovalStatus,
    DecidedBy,
    EventType,
    JsonObject,
    NotificationAttempt,
    NotificationChannel,
    NotificationStatus,
    RunStatus,
)
from lastro.history.redaction import redact_text, redact_to_json
from lastro.history.runs import apply_transition, select_run

RUN_NOT_RUNNING = "run_not_running"
APPROVAL_PENDING = "approval_pending"
APPROVAL_NOT_FOUND = "approval_not_found"
APPROVAL_NOT_APPROVED = "approval_not_approved"
FINGERPRINT_CHANGED = "fingerprint_changed"
END_RUN_REASON = "ended_at_approval_gate"

_DECIDED_STATUS = {
    ApprovalChoice.APPROVE: ApprovalStatus.APPROVED,
    ApprovalChoice.SKIP: ApprovalStatus.SKIPPED,
    ApprovalChoice.END_RUN: ApprovalStatus.ENDED,
}


def approval_from_row(row: sqlite3.Row) -> ApprovalRecord:
    decision = row["decision"]
    decided_by = row["decided_by"]
    return ApprovalRecord(
        id=row["id"],
        run_id=row["run_id"],
        action_id=row["action_id"],
        fingerprint=row["action_fingerprint"],
        status=ApprovalStatus(row["status"]),
        decision=ApprovalChoice(decision) if decision is not None else None,
        decided_by=DecidedBy(decided_by) if decided_by is not None else None,
        decision_reason=row["decision_reason"],
        payload=cast(JsonObject, json.loads(row["payload_json"])),
        requested_at=row["requested_at"],
        expires_at=row["expires_at"],
        decided_at=row["decided_at"],
        invalidated_at=row["invalidated_at"],
        consumed_at=row["consumed_at"],
        execution_correlation_id=row["execution_correlation_id"],
    )


def _select(conn: sqlite3.Connection, approval_id: str) -> ApprovalRecord:
    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    return approval_from_row(row)


def select_approval(
    conn: sqlite3.Connection, action_id: str, run_id: str | None = None
) -> ApprovalRecord | None:
    if run_id is not None:
        row = conn.execute(
            "SELECT * FROM approvals WHERE run_id = ? AND action_id = ?", (run_id, action_id)
        ).fetchone()
    else:
        # O action id é aleatório; com colisão entre runs, o pedido pendente tem prioridade.
        row = conn.execute(
            "SELECT * FROM approvals WHERE action_id = ? "
            "ORDER BY status = 'pending' DESC, requested_at DESC LIMIT 1",
            (action_id,),
        ).fetchone()
    return approval_from_row(row) if row is not None else None


def select_approvals(conn: sqlite3.Connection, run_id: str) -> list[ApprovalRecord]:
    rows = conn.execute(
        "SELECT * FROM approvals WHERE run_id = ? ORDER BY requested_at, id", (run_id,)
    ).fetchall()
    return [approval_from_row(row) for row in rows]


def select_pending(conn: sqlite3.Connection, run_id: str) -> ApprovalRecord | None:
    row = conn.execute(
        "SELECT * FROM approvals WHERE run_id = ? AND status = 'pending'", (run_id,)
    ).fetchone()
    return approval_from_row(row) if row is not None else None


def _note(text: str | None) -> str | None:
    if text is None:
        return None
    if len(text) > MAX_APPROVAL_NOTE_LENGTH:
        raise ApprovalError(
            "note_too_long", f"The note accepts up to {MAX_APPROVAL_NOTE_LENGTH} characters."
        )
    return redact_text(text)


def _append_decided(
    conn: sqlite3.Connection,
    record: ApprovalRecord,
    status: ApprovalStatus,
    timestamp: str,
    extra: dict[str, object] | None = None,
) -> None:
    payload: dict[str, object] = {
        "action_id": record.action_id,
        "fingerprint": record.fingerprint,
        "status": status.value,
        "summary": record.summary,
        "timestamp": timestamp,
        **(extra or {}),
    }
    insert_event(
        conn, record.run_id, prepare_event(EventType.APPROVAL_DECIDED, payload=payload), timestamp
    )


def create_request(
    conn: sqlite3.Connection,
    run_id: str,
    action_id: str,
    fingerprint: str,
    payload: JsonObject,
    now: datetime,
) -> ApprovalRecord:
    """Persiste pedido, evento e a espera da run antes de qualquer efeito no alvo."""
    timestamp = db_timestamp(now)
    expire_due(conn, now, run_id)
    run = select_run(conn, run_id)
    if run.status != RunStatus.RUNNING:
        raise ApprovalError(RUN_NOT_RUNNING, f"run is {run.status.value}")
    if select_pending(conn, run_id) is not None:
        raise ApprovalError(APPROVAL_PENDING, "Another approval is pending for this run.")
    stored = cast(JsonObject, redact_to_json(payload).value)
    approval_id = "approval_" + uuid.uuid4().hex
    expires_at = db_timestamp(now + timedelta(seconds=APPROVAL_TTL_SECONDS))
    conn.execute(
        "INSERT INTO approvals (id, run_id, action_id, action_fingerprint, status, payload_json, "
        "requested_at, expires_at) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)",
        (
            approval_id,
            run_id,
            action_id,
            fingerprint,
            json.dumps(stored, ensure_ascii=False, sort_keys=True),
            timestamp,
            expires_at,
        ),
    )
    record = _select(conn, approval_id)
    event = prepare_event(
        EventType.APPROVAL_REQUESTED,
        payload={
            "action_id": action_id,
            "fingerprint": fingerprint,
            "summary": record.summary,
            "expires_at": expires_at,
            "command": f"lastro approve {action_id}",
        },
    )
    insert_event(conn, run_id, event, timestamp)
    apply_transition(conn, run_id, RunStatus.AWAITING_APPROVAL, "approval", timestamp)
    return record


def _resume(conn: sqlite3.Connection, run_id: str, timestamp: str) -> None:
    if select_run(conn, run_id).status == RunStatus.AWAITING_APPROVAL:
        apply_transition(conn, run_id, RunStatus.RUNNING, None, timestamp)


def expire_due(
    conn: sqlite3.Connection, now: datetime, run_id: str | None = None
) -> list[ApprovalRecord]:
    """Pedidos vencidos viram skip do sistema por no_response; silêncio nunca aprova."""
    timestamp = db_timestamp(now)
    query = "SELECT * FROM approvals WHERE status = 'pending' AND expires_at <= ?"
    params: tuple[str, ...] = (timestamp,)
    if run_id is not None:
        query += " AND run_id = ?"
        params = (timestamp, run_id)
    expired: list[ApprovalRecord] = []
    for row in conn.execute(query, params).fetchall():
        record = approval_from_row(row)
        changed = conn.execute(
            "UPDATE approvals SET status = 'expired', decision = 'skip', decided_by = 'system', "
            "decision_reason = ?, decided_at = ? WHERE id = ? AND status = 'pending'",
            (NO_RESPONSE_REASON, timestamp, record.id),
        ).rowcount
        if changed != 1:
            continue
        _append_decided(
            conn,
            record,
            ApprovalStatus.EXPIRED,
            timestamp,
            {"decision": "skip", "decided_by": "system", "reason": NO_RESPONSE_REASON},
        )
        if not select_run(conn, record.run_id).status.is_terminal:
            _resume(conn, record.run_id, timestamp)
        expired.append(_select(conn, record.id))
    return expired


def decide(
    conn: sqlite3.Connection,
    action_id: str,
    choice: ApprovalChoice,
    decided_by: DecidedBy,
    now: datetime,
    *,
    note: str | None = None,
    run_id: str | None = None,
    proof_nonce_sha256: str | None = None,
) -> ApprovalRecord:
    """Decisão externa ao modelo: grava uma única vez, com CAS sobre o estado pendente."""
    timestamp = db_timestamp(now)
    reason = _note(note)
    expire_due(conn, now, run_id)
    record = select_approval(conn, action_id, run_id)
    if record is None or record.status != ApprovalStatus.PENDING:
        raise ApprovalNotPendingError(action_id)
    if select_run(conn, record.run_id).status != RunStatus.AWAITING_APPROVAL:
        raise ApprovalNotPendingError(action_id)
    status = _DECIDED_STATUS[choice]
    changed = conn.execute(
        "UPDATE approvals SET status = ?, decision = ?, decided_by = ?, decision_reason = ?, "
        "decided_at = ?, proof_nonce_sha256 = ? "
        "WHERE id = ? AND status = 'pending' AND expires_at > ?",
        (
            status.value,
            choice.value,
            decided_by.value,
            reason,
            timestamp,
            proof_nonce_sha256,
            record.id,
            timestamp,
        ),
    ).rowcount
    if changed != 1:
        raise ApprovalNotPendingError(action_id)
    _append_decided(
        conn,
        record,
        status,
        timestamp,
        {"decision": choice.value, "decided_by": decided_by.value, "reason": reason},
    )
    if choice == ApprovalChoice.END_RUN:
        apply_transition(conn, record.run_id, RunStatus.CANCELLED, END_RUN_REASON, timestamp)
    else:
        apply_transition(conn, record.run_id, RunStatus.RUNNING, None, timestamp)
    return _select(conn, record.id)


def invalidate(
    conn: sqlite3.Connection, record: ApprovalRecord, changed_field: str, now: datetime
) -> ApprovalRecord:
    timestamp = db_timestamp(now)
    updated = conn.execute(
        "UPDATE approvals SET status = 'invalidated', invalidated_at = ? "
        "WHERE id = ? AND status = 'approved' AND consumed_at IS NULL",
        (timestamp, record.id),
    ).rowcount
    if updated != 1:
        raise ApprovalError(APPROVAL_NOT_APPROVED, "The approval is no longer usable.")
    _append_decided(
        conn,
        record,
        ApprovalStatus.INVALIDATED,
        timestamp,
        {"decided_by": "system", "changed_field": changed_field},
    )
    return _select(conn, record.id)


def consume(
    conn: sqlite3.Connection,
    run_id: str,
    action_id: str,
    fingerprint: str,
    correlation_id: str,
    now: datetime,
) -> ApprovalRecord:
    """Marca o uso único antes do efeito; a atualização condicional impede consumo concorrente."""
    timestamp = db_timestamp(now)
    record = select_approval(conn, action_id, run_id)
    if record is None:
        raise ApprovalError(APPROVAL_NOT_FOUND, f"Unknown approval id {action_id}.")
    if record.status != ApprovalStatus.APPROVED:
        raise ApprovalError(APPROVAL_NOT_APPROVED, f"Approval {action_id} is {record.status}.")
    if record.fingerprint != fingerprint:
        raise ApprovalError(FINGERPRINT_CHANGED, "The action changed after approval.")
    if select_run(conn, run_id).status != RunStatus.RUNNING:
        raise ApprovalError(RUN_NOT_RUNNING, "The run is not running.")
    updated = conn.execute(
        "UPDATE approvals SET status = 'consumed', consumed_at = ?, execution_correlation_id = ? "
        "WHERE id = ? AND status = 'approved' AND consumed_at IS NULL "
        "AND action_fingerprint = ?",
        (timestamp, correlation_id, record.id, fingerprint),
    ).rowcount
    if updated != 1:
        raise ApprovalError(APPROVAL_NOT_APPROVED, f"Approval {action_id} was already used.")
    _append_decided(
        conn,
        record,
        ApprovalStatus.CONSUMED,
        timestamp,
        {"decided_by": "system", "execution_correlation_id": correlation_id},
    )
    return _select(conn, record.id)


def mark_execution_failed(
    conn: sqlite3.Connection, run_id: str, action_id: str, error_code: str, now: datetime
) -> ApprovalRecord:
    """Falha depois do consumo nunca libera replay: o resultado fica para verificação manual."""
    timestamp = db_timestamp(now)
    record = select_approval(conn, action_id, run_id)
    if record is None or record.status != ApprovalStatus.CONSUMED:
        raise ApprovalError(APPROVAL_NOT_APPROVED, f"Approval {action_id} was not consumed.")
    conn.execute(
        "UPDATE approvals SET status = 'execution_failed' WHERE id = ? AND status = 'consumed'",
        (record.id,),
    )
    safe_code = redact_text(error_code)[:200]
    _append_decided(
        conn,
        record,
        ApprovalStatus.EXECUTION_FAILED,
        timestamp,
        {"decided_by": "system", "error_code": safe_code},
    )
    return _select(conn, record.id)


def _attempt_from_row(row: sqlite3.Row, action_id: str) -> NotificationAttempt:
    return NotificationAttempt(
        approval_id=action_id,
        channel=NotificationChannel(row["channel"]),
        status=NotificationStatus(row["status"]),
        requested_at=row["requested_at"],
        completed_at=row["completed_at"],
        latency_ms=row["latency_ms"],
        error_code=row["error_code"],
    )


def record_notification(
    conn: sqlite3.Connection,
    record: ApprovalRecord,
    channel: NotificationChannel,
    status: NotificationStatus,
    requested_at: str,
    completed_at: str,
    latency_ms: int,
    error_code: str | None,
    attempt_key: str,
) -> NotificationAttempt:
    """Tentativa idempotente por chave; degradação gera evento, nunca muda a decisão."""
    safe_code = redact_text(error_code)[:200] if error_code is not None else None
    cursor = conn.execute(
        "INSERT OR IGNORE INTO approval_notifications (id, approval_id, channel, status, "
        "attempt_key, requested_at, completed_at, latency_ms, error_code) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "notification_" + uuid.uuid4().hex,
            record.id,
            channel.value,
            status.value,
            attempt_key,
            requested_at,
            completed_at,
            max(latency_ms, 0),
            safe_code,
        ),
    )
    if cursor.rowcount == 1 and status != NotificationStatus.DELIVERED:
        insert_event(
            conn,
            record.run_id,
            prepare_event(
                EventType.NOTIFICATION_DEGRADED,
                payload={
                    "action_id": record.action_id,
                    "channel": channel.value,
                    "status": status.value,
                    "error_code": safe_code,
                    "fallback": "tail_banner_and_bell",
                },
            ),
            completed_at,
        )
    row = conn.execute(
        "SELECT * FROM approval_notifications WHERE approval_id = ? AND channel = ? "
        "AND attempt_key = ?",
        (record.id, channel.value, attempt_key),
    ).fetchone()
    return _attempt_from_row(row, record.action_id)


def select_notifications(
    conn: sqlite3.Connection, record: ApprovalRecord
) -> list[NotificationAttempt]:
    rows = conn.execute(
        "SELECT * FROM approval_notifications WHERE approval_id = ? ORDER BY requested_at, id",
        (record.id,),
    ).fetchall()
    return [_attempt_from_row(row, record.action_id) for row in rows]


def nonce_digest(nonce: str) -> str:
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()
