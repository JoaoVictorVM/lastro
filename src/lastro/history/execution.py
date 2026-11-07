"""Persistência transacional do controle de execução, sempre em transações curtas.

Reserva e contador mudam na mesma transação; o primeiro motivo de parada é imutável (há trigger
no banco); nenhum valor aqui vem do modelo sem passar pela redação.
"""

import json
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Any, cast

from lastro.config.limits import RunLimits, limits_from_snapshot
from lastro.execution.models import (
    ACTION_LIMIT,
    ATTEMPT_LIMIT,
    DRAIN_SECONDS,
    DURATION_LIMIT,
    FINALIZE_SECONDS,
    NO_PROGRESS,
    ActionOutcome,
    ExecutionState,
    Notice,
    PendingItem,
    Reconciliation,
    ReconciliationState,
    Reservation,
    StopLatch,
    WriteIntent,
    WriteIntentInput,
    WriteState,
    warning_notice,
)
from lastro.execution.progress import new_keys
from lastro.history.database import db_timestamp
from lastro.history.errors import HistoryError, InvalidTransitionError
from lastro.history.events import insert_event, prepare_event, select_events
from lastro.history.models import CleanupStatus, EventType, JsonObject, RunRecord
from lastro.history.redaction import canonical_json, redact, redact_text
from lastro.history.runs import select_run
from lastro.timeutil import parse_timestamp

UNRESOLVED_WRITE_STATES = (WriteState.SENDING.value, WriteState.RESPONSE_UNKNOWN.value)
WITHDRAWN_REASON = "cancelled"


def limits_of(run: RunRecord) -> RunLimits:
    return limits_from_snapshot(run.config_snapshot.get("run_limits"))


def deadline_for(started_at: str, limits: RunLimits) -> str:
    return db_timestamp(parse_timestamp(started_at) + timedelta(minutes=limits.max_minutes))


def init_control(conn: sqlite3.Connection, run: RunRecord) -> None:
    """Linha de controle da run; runs legadas a recebem na primeira leitura validada."""
    conn.execute(
        "INSERT OR IGNORE INTO execution_controls (run_id, deadline_at) VALUES (?, ?)",
        (run.id, deadline_for(run.started_at, limits_of(run))),
    )


def _state_from_row(row: sqlite3.Row, limits: RunLimits) -> ExecutionState:
    return ExecutionState(
        run_id=row["run_id"],
        deadline_at=row["deadline_at"],
        max_minutes=limits.max_minutes,
        max_actions=limits.max_actions,
        max_attempts=limits.max_attempts,
        max_no_progress_actions=limits.max_no_progress_actions,
        actions_reserved=row["actions_reserved"],
        actions_completed=row["actions_completed"],
        no_progress_count=row["no_progress_count"],
        event_cursor=row["event_cursor"],
        duration_warned=bool(row["duration_warned"]),
        actions_warned=bool(row["actions_warned"]),
        stop_status=row["stop_status"],
        stop_reason=row["stop_reason"],
        stop_requested_at=row["stop_requested_at"],
        drain_deadline_at=row["drain_deadline_at"],
        finalize_deadline_at=row["finalize_deadline_at"],
        revision=row["revision"],
    )


def select_state(conn: sqlite3.Connection, run_id: str) -> ExecutionState:
    """Leitura sem efeito: run legada sem linha recebe a projeção dos defaults, sem contadores."""
    run = select_run(conn, run_id)
    limits = limits_of(run)
    row = conn.execute("SELECT * FROM execution_controls WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        return ExecutionState(
            run_id=run_id,
            deadline_at=deadline_for(run.started_at, limits),
            max_minutes=limits.max_minutes,
            max_actions=limits.max_actions,
            max_attempts=limits.max_attempts,
            max_no_progress_actions=limits.max_no_progress_actions,
            actions_reserved=0,
            actions_completed=0,
            no_progress_count=0,
            event_cursor=0,
            duration_warned=False,
            actions_warned=False,
            stop_status=None,
            stop_reason=None,
            stop_requested_at=None,
            drain_deadline_at=None,
            finalize_deadline_at=None,
            revision=0,
        )
    return _state_from_row(row, limits)


def _writable_state(conn: sqlite3.Connection, run_id: str) -> ExecutionState:
    init_control(conn, select_run(conn, run_id))
    return select_state(conn, run_id)


def _event(
    conn: sqlite3.Connection, run_id: str, kind: EventType, payload: JsonObject, timestamp: str
) -> None:
    insert_event(conn, run_id, prepare_event(kind, payload=payload), timestamp)


def _progress(state_row: sqlite3.Row) -> dict[str, Any]:
    data: object = json.loads(state_row["progress_json"])
    return cast(dict[str, Any], data) if isinstance(data, dict) else {}


def _attempt_no(
    conn: sqlite3.Connection, run_id: str, operation_key: str, since_ordinal: int
) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS failures FROM execution_actions WHERE run_id = ? "
        "AND operation_key = ? AND state = 'failed' AND ordinal > ?",
        (run_id, operation_key, since_ordinal),
    ).fetchone()
    return int(row["failures"]) + 1


def reserve(
    conn: sqlite3.Connection,
    run_id: str,
    action_id: str,
    tool: str,
    operation_key: str | None,
    timestamp: str,
) -> Reservation:
    """Reserva durável antes de validar argumentos; admissão fechada não consome unidade."""
    run = select_run(conn, run_id)
    state = _writable_state(conn, run_id)
    if run.status.is_terminal or state.admission_closed:
        return Reservation(
            admitted=False,
            action_id=action_id,
            stop_status=state.stop_status or (run.status.value if run.status.is_terminal else None),
            stop_reason=state.stop_reason or run.status_reason or ACTION_LIMIT,
        )
    ordinal = state.actions_reserved + 1
    row = conn.execute("SELECT * FROM execution_controls WHERE run_id = ?", (run_id,)).fetchone()
    since = int(_progress(row).get("last_progress_ordinal", 0))
    attempt = _attempt_no(conn, run_id, operation_key, since) if operation_key else None
    conn.execute(
        "INSERT INTO execution_actions (action_id, run_id, ordinal, tool_name, operation_key, "
        "attempt_no, reserved_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (action_id, run_id, ordinal, tool, operation_key, attempt, timestamp),
    )
    warning: Notice | None = None
    warn = not state.actions_warned and ordinal * 5 >= state.max_actions * 4
    conn.execute(
        "UPDATE execution_controls SET actions_reserved = ?, actions_warned = ?, "
        "revision = revision + 1 WHERE run_id = ?",
        (ordinal, 1 if warn or state.actions_warned else 0, run_id),
    )
    if warn:
        warning = warning_notice("actions", ordinal, state.max_actions)
        _event(conn, run_id, EventType.EXECUTION_WARNING, warning.as_json(), timestamp)
    return Reservation(
        admitted=True,
        action_id=action_id,
        ordinal=ordinal,
        last=ordinal >= state.max_actions,
        attempt_no=attempt,
        warning=warning,
    )


def warn_duration(conn: sqlite3.Connection, run_id: str, now: datetime) -> Notice | None:
    """Aviso único de 80% da duração; idempotente entre processos pela coluna durável."""
    state = _writable_state(conn, run_id)
    run = select_run(conn, run_id)
    if state.duration_warned or state.stopped or run.status.is_terminal:
        return None
    started = parse_timestamp(run.started_at)
    elapsed = (now - started).total_seconds()
    if elapsed * 5 < state.max_minutes * 60 * 4:
        return None
    conn.execute(
        "UPDATE execution_controls SET duration_warned = 1, revision = revision + 1 "
        "WHERE run_id = ? AND duration_warned = 0",
        (run_id,),
    )
    notice = warning_notice("duration", int(elapsed // 60), state.max_minutes)
    _event(conn, run_id, EventType.EXECUTION_WARNING, notice.as_json(), db_timestamp(now))
    return notice


def _observed(
    state: ExecutionState, reason: str, now: datetime, started_at: str
) -> tuple[int, int]:
    if reason == ACTION_LIMIT:
        return state.actions_reserved, state.max_actions
    if reason == NO_PROGRESS:
        return state.no_progress_count, state.max_no_progress_actions
    if reason == ATTEMPT_LIMIT:
        return state.max_attempts, state.max_attempts
    if reason == DURATION_LIMIT:
        elapsed = int((now - parse_timestamp(started_at)).total_seconds() // 60)
        return elapsed, state.max_minutes
    return 0, 0


def latch(
    conn: sqlite3.Connection,
    run_id: str,
    status: str,
    reason: str,
    now: datetime,
    *,
    origin: str = "owner",
) -> StopLatch:
    """Primeiro latch vence; sinal concorrente é auditado sem substituir o motivo terminal."""
    run = select_run(conn, run_id)
    state = _writable_state(conn, run_id)
    timestamp = db_timestamp(now)
    if state.stopped or run.status.is_terminal:
        if not run.status.is_terminal and state.stop_reason != reason:
            kind = (
                EventType.CANCELLATION_REQUESTED if status == "cancelled" else EventType.LIMIT_HIT
            )
            _event(
                conn,
                run_id,
                kind,
                {"reason": reason, "superseded_by": state.stop_reason, "origin": origin},
                timestamp,
            )
        return StopLatch(state, won=False)
    if status not in ("cancelled", "limit_reached", "interrupted"):
        raise InvalidTransitionError(f"invalid stop status {status}")
    drain = db_timestamp(now + timedelta(seconds=DRAIN_SECONDS))
    finalize = db_timestamp(now + timedelta(seconds=FINALIZE_SECONDS))
    conn.execute(
        "UPDATE execution_controls SET stop_status = ?, stop_reason = ?, stop_requested_at = ?, "
        "drain_deadline_at = ?, finalize_deadline_at = ?, revision = revision + 1 "
        "WHERE run_id = ? AND stop_status IS NULL",
        (status, redact_text(reason), timestamp, drain, finalize, run_id),
    )
    if status == "cancelled":
        _event(
            conn,
            run_id,
            EventType.CANCELLATION_REQUESTED,
            {"reason": reason, "origin": origin},
            timestamp,
        )
    else:
        observed, limit = _observed(state, reason, now, run.started_at)
        _event(
            conn,
            run_id,
            EventType.LIMIT_HIT,
            {
                "dimension": reason,
                "observed": observed,
                "limit": limit,
                "cursor": state.event_cursor,
                "status": status,
            },
            timestamp,
        )
    return StopLatch(select_state(conn, run_id), won=True)


def complete(
    conn: sqlite3.Connection,
    run_id: str,
    action_id: str,
    *,
    failed: bool,
    result_code: str | None,
    page: str | None,
    now: datetime,
) -> ActionOutcome:
    """Fecha a ação uma vez: consome eventos após o cursor, aplica progresso e tentativas."""
    timestamp = db_timestamp(now)
    action = conn.execute(
        "SELECT * FROM execution_actions WHERE action_id = ? AND run_id = ?", (action_id, run_id)
    ).fetchone()
    state = _writable_state(conn, run_id)
    if action is None or action["state"] != "reserved":
        return ActionOutcome(
            progressed=False,
            no_progress_count=state.no_progress_count,
            attempt_no=action["attempt_no"] if action is not None else None,
            stop_status=state.stop_status,
            stop_reason=state.stop_reason,
        )
    row = conn.execute("SELECT * FROM execution_controls WHERE run_id = ?", (run_id,)).fetchone()
    progress = _progress(row)
    seen = set(cast(list[str], progress.get("seen", [])))
    events = select_events(conn, run_id, after_sequence=state.event_cursor)
    keys = new_keys(seen, [(event.event_type.value, event.payload) for event in events], page)
    cursor = events[-1].sequence if events else state.event_cursor
    progressed = bool(keys)
    no_progress = 0 if progressed else state.no_progress_count + 1
    if progressed:
        progress["seen"] = sorted(seen | set(keys))
        progress["last_progress_ordinal"] = action["ordinal"]
    conn.execute(
        "UPDATE execution_actions SET state = ?, completed_at = ?, result_code = ? "
        "WHERE action_id = ? AND state = 'reserved'",
        (
            "failed" if failed else "completed",
            timestamp,
            redact_text(result_code)[:200] if result_code else None,
            action_id,
        ),
    )
    conn.execute(
        "UPDATE execution_controls SET actions_completed = actions_completed + 1, "
        "no_progress_count = ?, event_cursor = ?, progress_json = ?, revision = revision + 1 "
        "WHERE run_id = ?",
        (no_progress, cursor, canonical_json(progress), run_id),
    )
    attempt = action["attempt_no"]
    reason: str | None = None
    if failed and attempt is not None and attempt >= state.max_attempts:
        reason = ATTEMPT_LIMIT
    elif no_progress >= state.max_no_progress_actions:
        reason = NO_PROGRESS
    elif action["ordinal"] >= state.max_actions:
        reason = ACTION_LIMIT
    latched = False
    if reason is not None and not state.stopped:
        latched = latch(conn, run_id, "limit_reached", reason, now).won
    final = select_state(conn, run_id)
    return ActionOutcome(
        progressed=progressed,
        no_progress_count=no_progress,
        attempt_no=attempt,
        stop_status=final.stop_status,
        stop_reason=final.stop_reason,
        latched=latched,
    )


# Intenções de escrita ------------------------------------------------------------------------


def _values(text: str | None) -> dict[str, str] | None:
    if text is None:
        return None
    data: object = json.loads(text)
    if not isinstance(data, dict):
        return None
    return {str(key): str(value) for key, value in cast(dict[object, object], data).items()}


def intent_from_row(row: sqlite3.Row) -> WriteIntent:
    return WriteIntent(
        id=row["id"],
        run_id=row["run_id"],
        action_id=row["action_id"],
        binding_id=row["binding_id"],
        operation=row["operation"],
        entity=row["entity"],
        record_id=row["record_id"],
        tenant=row["tenant"],
        detail_path=row["detail_path"],
        before=_values(row["before_json"]),
        intended=_values(row["intended_json"]),
        comparable=bool(row["comparable"]),
        state=WriteState(row["state"]),
        response_status=row["response_status"],
        created_at=row["created_at"],
        sent_at=row["sent_at"],
        resolved_at=row["resolved_at"],
    )


def _comparable_values(values: dict[str, str] | None) -> str | None:
    """Valores persistíveis somente quando a redação não os alterou."""
    if values is None:
        return None
    return canonical_json(dict(sorted(values.items())))


def insert_intent(conn: sqlite3.Connection, data: WriteIntentInput, timestamp: str) -> WriteIntent:
    """Journal antes do request: valor alterado pela redação torna a intenção não comparável."""
    select_run(conn, data.run_id)
    before = data.before
    intended = data.intended
    redacted_before = cast(object, redact(before)) if before is not None else None
    redacted_intended = cast(object, redact(intended)) if intended is not None else None
    comparable = (
        data.comparable
        and data.record_id is not None
        and intended is not None
        and redacted_intended == intended
        and (before is None or redacted_before == before)
    )
    intent_id = "wi_" + secrets.token_hex(8)
    conn.execute(
        "INSERT INTO write_intents (id, run_id, action_id, binding_id, operation, entity, "
        "record_id, tenant, detail_path, before_json, intended_json, comparable, state, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?)",
        (
            intent_id,
            data.run_id,
            data.action_id,
            redact_text(data.binding_id),
            data.operation,
            redact_text(data.entity),
            redact_text(data.record_id) if data.record_id is not None else None,
            redact_text(data.tenant),
            redact_text(data.detail_path) if data.detail_path is not None else None,
            _comparable_values(cast(dict[str, str], redacted_before))
            if redacted_before is not None
            else None,
            _comparable_values(cast(dict[str, str], redacted_intended))
            if redacted_intended is not None
            else None,
            1 if comparable else 0,
            timestamp,
        ),
    )
    return select_intent(conn, intent_id)


def select_intent(conn: sqlite3.Connection, intent_id: str) -> WriteIntent:
    row = conn.execute("SELECT * FROM write_intents WHERE id = ?", (intent_id,)).fetchone()
    if row is None:
        raise HistoryError(f"unknown write intent {intent_id}")
    return intent_from_row(row)


def mark_sending(conn: sqlite3.Connection, intent_id: str, timestamp: str) -> WriteIntent:
    """Persistido antes de liberar a rede: crash depois daqui é ambiguidade conservadora."""
    changed = conn.execute(
        "UPDATE write_intents SET state = 'sending', sent_at = ? "
        "WHERE id = ? AND state = 'prepared'",
        (timestamp, intent_id),
    ).rowcount
    if changed != 1:
        raise HistoryError(f"write intent {intent_id} is not prepared")
    return select_intent(conn, intent_id)


def record_response(
    conn: sqlite3.Connection, intent_id: str, status: int, timestamp: str
) -> WriteIntent:
    conn.execute(
        "UPDATE write_intents SET state = 'response_known', response_status = ?, "
        "resolved_at = ? WHERE id = ? AND state IN ('sending', 'response_unknown')",
        (status, timestamp, intent_id),
    )
    return select_intent(conn, intent_id)


def mark_unknown(conn: sqlite3.Connection, run_id: str, timestamp: str) -> list[WriteIntent]:
    """Escritas enviadas sem resposta ao fim da drenagem: efeito desconhecido, nunca repetido."""
    rows = conn.execute(
        "SELECT id FROM write_intents WHERE run_id = ? AND state = 'sending'", (run_id,)
    ).fetchall()
    conn.execute(
        "UPDATE write_intents SET state = 'response_unknown', resolved_at = ? "
        "WHERE run_id = ? AND state = 'sending'",
        (timestamp, run_id),
    )
    return [select_intent(conn, row["id"]) for row in rows]


def select_intents(conn: sqlite3.Connection, run_id: str) -> list[WriteIntent]:
    rows = conn.execute(
        "SELECT * FROM write_intents WHERE run_id = ? ORDER BY created_at, id", (run_id,)
    ).fetchall()
    return [intent_from_row(row) for row in rows]


# Vínculos e reconciliação ----------------------------------------------------------------------


def insert_link(conn: sqlite3.Connection, child: str, parent: str, timestamp: str) -> None:
    select_run(conn, parent)
    if parent in ancestors(conn, parent) or child in ancestors(conn, parent):
        raise HistoryError("resume chain would create a cycle")
    conn.execute(
        "INSERT INTO run_links (child_run_id, parent_run_id, kind, created_at) "
        "VALUES (?, ?, 'resume', ?)",
        (child, parent, timestamp),
    )


def select_parent(conn: sqlite3.Connection, run_id: str) -> str | None:
    row = conn.execute(
        "SELECT parent_run_id FROM run_links WHERE child_run_id = ?", (run_id,)
    ).fetchone()
    return str(row["parent_run_id"]) if row is not None else None


def ancestors(conn: sqlite3.Connection, run_id: str) -> list[str]:
    """Pais em ordem do mais próximo ao mais antigo; ciclo interrompe a busca."""
    chain: list[str] = []
    current = select_parent(conn, run_id)
    while current is not None and current not in chain and current != run_id:
        chain.append(current)
        current = select_parent(conn, current)
    return chain


def reconciliation_from_row(row: sqlite3.Row) -> Reconciliation:
    return Reconciliation(
        id=row["id"],
        resume_run_id=row["resume_run_id"],
        source_write_id=row["source_write_id"],
        state=ReconciliationState(row["state"]),
        reason=row["reason"],
        observation_artifact_id=row["observation_artifact_id"],
        replacement_write_id=row["replacement_write_id"],
        created_at=row["created_at"],
    )


def select_reconciliations(conn: sqlite3.Connection, run_id: str) -> list[Reconciliation]:
    rows = conn.execute(
        "SELECT * FROM write_reconciliations WHERE resume_run_id = ? ORDER BY created_at, id",
        (run_id,),
    ).fetchall()
    return [reconciliation_from_row(row) for row in rows]


def _resolved_sources(conn: sqlite3.Connection, run_ids: list[str]) -> set[str]:
    """Intenções resolvidas em qualquer run da cadeia: já aplicadas ou substituídas."""
    if not run_ids:
        return set()
    placeholders = ", ".join("?" for _ in run_ids)
    rows = conn.execute(
        f"SELECT source_write_id FROM write_reconciliations WHERE resume_run_id IN "
        f"({placeholders}) AND (state = 'already_applied' OR replacement_write_id IS NOT NULL)",
        tuple(run_ids),
    ).fetchall()
    return {str(row["source_write_id"]) for row in rows}


def unresolved_writes(conn: sqlite3.Connection, run_id: str) -> list[WriteIntent]:
    """Pendências de escrita herdadas dos ancestrais, deduplicadas pela intenção original."""
    chain = ancestors(conn, run_id)
    resolved = _resolved_sources(conn, [run_id, *chain])
    found: dict[str, WriteIntent] = {}
    for ancestor in chain:
        for intent in select_intents(conn, ancestor):
            if intent.state.value in UNRESOLVED_WRITE_STATES and intent.id not in resolved:
                found.setdefault(intent.id, intent)
    return list(found.values())


def insert_reconciliation(
    conn: sqlite3.Connection,
    resume_run_id: str,
    source_write_id: str,
    state: ReconciliationState,
    reason: str,
    timestamp: str,
    *,
    observation_artifact_id: str | None = None,
) -> Reconciliation:
    """Uma verificação por intenção e run; repetir devolve o registro existente."""
    source = select_intent(conn, source_write_id)
    if source.run_id not in ancestors(conn, resume_run_id):
        raise HistoryError("write intent does not belong to an ancestor run")
    existing = conn.execute(
        "SELECT * FROM write_reconciliations WHERE resume_run_id = ? AND source_write_id = ?",
        (resume_run_id, source_write_id),
    ).fetchone()
    if existing is not None:
        return reconciliation_from_row(existing)
    reconciliation_id = "wr_" + secrets.token_hex(8)
    safe_reason = redact_text(reason)[:500] or state.value
    conn.execute(
        "INSERT INTO write_reconciliations (id, resume_run_id, source_write_id, state, reason, "
        "observation_artifact_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            reconciliation_id,
            resume_run_id,
            source_write_id,
            state.value,
            safe_reason,
            observation_artifact_id,
            timestamp,
        ),
    )
    _event(
        conn,
        resume_run_id,
        EventType.WRITE_RECONCILED,
        {
            "source_write_id": source_write_id,
            "state": state.value,
            "record_id": source.record_id,
            "entity": source.entity,
            "evidence_artifact_id": observation_artifact_id,
            "write_repeated": False,
        },
        timestamp,
    )
    row = conn.execute(
        "SELECT * FROM write_reconciliations WHERE id = ?", (reconciliation_id,)
    ).fetchone()
    return reconciliation_from_row(row)


def link_replacement(
    conn: sqlite3.Connection, resume_run_id: str, source_write_id: str, replacement_id: str
) -> None:
    conn.execute(
        "UPDATE write_reconciliations SET replacement_write_id = ? WHERE resume_run_id = ? "
        "AND source_write_id = ? AND state = 'ready_for_review' AND replacement_write_id IS NULL",
        (replacement_id, resume_run_id, source_write_id),
    )


# Pendências ------------------------------------------------------------------------------------


def _withdrawn_approvals(conn: sqlite3.Connection, run_id: str) -> list[PendingItem]:
    rows = conn.execute(
        "SELECT json_extract(payload_json, '$.action_id') AS action_id FROM events "
        "WHERE run_id = ? AND event_type = 'approval_decided' "
        "AND (json_extract(payload_json, '$.reason') = ? "
        "OR json_extract(payload_json, '$.changed_field') = ?) ORDER BY sequence",
        (run_id, WITHDRAWN_REASON, WITHDRAWN_REASON),
    ).fetchall()
    return [
        PendingItem(
            kind="approval",
            reference=str(row["action_id"]),
            state="withdrawn",
            requires_manual_review=False,
            detail="Approval request withdrawn when the run stopped; it has no authority later.",
        )
        for row in rows
        if row["action_id"] is not None
    ]


def _write_item(intent: WriteIntent, run_id: str | None) -> PendingItem:
    record = intent.record_id or "unknown"
    if intent.operation == "create" and intent.record_id is None:
        detail = f"Create of {intent.entity} without a reliable id; verify manually."
    else:
        detail = f"{intent.operation} of {intent.entity} {record} may have been applied."
    return PendingItem(
        kind="write",
        reference=intent.id,
        state=intent.state.value,
        requires_manual_review=True,
        detail=detail,
        run_id=run_id,
    )


def pending_items(conn: sqlite3.Connection, project_id: str, run_id: str) -> list[PendingItem]:
    """Pendências da run a partir do histórico durável; não dependem da despedida do agente."""
    from lastro.history.created_records import select_created_records
    from lastro.history.mission_plans import select_active_plan
    from lastro.history.reporting import select_criterion_results

    items: list[PendingItem] = []
    plan = select_active_plan(conn, run_id)
    if plan is not None:
        evaluated = {item.criterion_id for item in select_criterion_results(conn, run_id)}
        items += [
            PendingItem(
                kind="criterion",
                reference=criterion.id,
                state="not_evaluated",
                requires_manual_review=False,
                detail=criterion.text,
            )
            for criterion in plan.criteria
            if criterion.id not in evaluated
        ]
    items += [
        _write_item(intent, None)
        for intent in select_intents(conn, run_id)
        if intent.state.value in UNRESOLVED_WRITE_STATES
    ]
    items += [_write_item(intent, intent.run_id) for intent in unresolved_writes(conn, run_id)]
    for item in select_reconciliations(conn, run_id):
        if item.state in (ReconciliationState.CONFLICT, ReconciliationState.INCONCLUSIVE) or (
            item.state == ReconciliationState.READY_FOR_REVIEW and item.replacement_write_id is None
        ):
            items.append(
                PendingItem(
                    kind="reconciliation",
                    reference=item.source_write_id,
                    state=item.state.value,
                    requires_manual_review=True,
                    detail=item.reason,
                )
            )
    items += _withdrawn_approvals(conn, run_id)
    items += [
        PendingItem(
            kind="created_record",
            reference=record.id,
            state="cleanup_pending",
            requires_manual_review=True,
            detail=f"{record.entity_type} {record.system_identifier or 'unknown'}",
        )
        for record in select_created_records(
            conn, project_id, run_id=run_id, cleanup_status=CleanupStatus.PENDING
        )
    ]
    # Pendências reconciliadas nesta run deixam de aparecer como escrita herdada.
    seen: set[tuple[str, str]] = set()
    unique: list[PendingItem] = []
    for item in items:
        key = (item.kind, item.reference)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique
