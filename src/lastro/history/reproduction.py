"""Rodadas, runs filhas, relatos do reprodutor e vereditos, só dentro das transações da fachada.

Este é o único ponto que acrescenta eventos ao log de uma run já terminal, e apenas os três
tipos de reprodução; qualquer outro tipo continua recusado pela fachada.
"""

import json
import sqlite3
from collections.abc import Callable
from typing import cast
from uuid import uuid4

from lastro.history.errors import (
    REPRODUCTION_NOT_ELIGIBLE,
    REPRODUCTION_ROUND_ACTIVE,
    InvalidEventError,
    ReportingError,
    ReproductionError,
)
from lastro.history.events import insert_event, prepare_event
from lastro.history.models import (
    MAX_REPRODUCTION_NOTE_LENGTH,
    REPRODUCTION_EVENTS,
    AttemptDetail,
    EventType,
    FailureDomain,
    FindingClassification,
    JsonObject,
    JsonValue,
    ReproducerReport,
    ReproducerResult,
    ReproductionAttempt,
    ReproductionChild,
    ReproductionOutcome,
    ReproductionRound,
    ReproductionStatus,
    ReproductionVerdict,
    RoundStatus,
    VerdictInput,
    VerdictReason,
)
from lastro.history.redaction import canonical_json, redact_text, sha256_hex
from lastro.history.reporting import (
    INVALID_EVIDENCE,
    INVALID_REFERENCE,
    attempt_from_row,
    promote,
    require_running,
    run_artifacts,
    select_finding,
    select_findings,
    settle_inconclusive,
)
from lastro.history.runs import select_run

NOT_A_REPRODUCTION_RUN = "not_a_reproduction_run"
ALREADY_REPORTED = "already_reported"
INVALID_PRECONDITION_INDEX = "invalid_precondition_index"


def _round_from_row(row: sqlite3.Row, *, resumed: bool = False) -> ReproductionRound:
    return ReproductionRound(
        id=row["id"],
        run_id=row["run_id"],
        mode=row["mode"],
        explorer_executor=row["explorer_executor"],
        verifier_executor=row["verifier_executor"],
        status=RoundStatus(row["status"]),
        owner_pid=row["owner_pid"],
        created_at=row["created_at"],
        closed_at=row["closed_at"],
        resumed=resumed,
    )


def select_open_round(conn: sqlite3.Connection, run_id: str) -> ReproductionRound | None:
    row = conn.execute(
        "SELECT * FROM reproduction_rounds WHERE run_id = ? AND status = 'open'", (run_id,)
    ).fetchone()
    return _round_from_row(row) if row is not None else None


def select_round(conn: sqlite3.Connection, round_id: str) -> ReproductionRound:
    row = conn.execute("SELECT * FROM reproduction_rounds WHERE id = ?", (round_id,)).fetchone()
    if row is None:
        raise ReproductionError(REPRODUCTION_NOT_ELIGIBLE, f"unknown round: {round_id}")
    return _round_from_row(row)


def open_round(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    mode: str,
    explorer: str,
    verifier: str,
    owner_pid: int,
    alive: Callable[[int], bool],
    timestamp: str,
) -> ReproductionRound:
    """Abre a rodada da run, ou assume a rodada aberta cujo coordenador já não existe.

    A rodada assumida mantém modo e executores: as tentativas que faltam seguem o mesmo par.
    """
    run = select_run(conn, run_id)
    if not run.status.is_terminal:
        raise ReproductionError(REPRODUCTION_NOT_ELIGIBLE, f"Run {run_id} is still running.")
    current = select_open_round(conn, run_id)
    if current is not None:
        if current.owner_pid != owner_pid and alive(current.owner_pid):
            raise ReproductionError(
                REPRODUCTION_ROUND_ACTIVE,
                f"A reproduction round for run {run_id} is already in progress.",
            )
        conn.execute(
            "UPDATE reproduction_rounds SET owner_pid = ? WHERE id = ?", (owner_pid, current.id)
        )
        row = conn.execute(
            "SELECT * FROM reproduction_rounds WHERE id = ?", (current.id,)
        ).fetchone()
        return _round_from_row(row, resumed=True)
    round_id = "round_" + uuid4().hex[:16]
    conn.execute(
        "INSERT INTO reproduction_rounds (id, run_id, mode, explorer_executor, "
        "verifier_executor, status, owner_pid, created_at) VALUES (?, ?, ?, ?, ?, 'open', ?, ?)",
        (round_id, run_id, mode, explorer, verifier, owner_pid, timestamp),
    )
    return select_round(conn, round_id)


def close_round(
    conn: sqlite3.Connection, round_id: str, status: RoundStatus, timestamp: str
) -> ReproductionRound:
    if status == RoundStatus.OPEN:
        raise ReproductionError(
            REPRODUCTION_NOT_ELIGIBLE, "a round closes as completed or abandoned"
        )
    conn.execute(
        "UPDATE reproduction_rounds SET status = ?, closed_at = ? WHERE id = ? AND status = 'open'",
        (status.value, timestamp, round_id),
    )
    return select_round(conn, round_id)


def _child_from_row(row: sqlite3.Row) -> ReproductionChild:
    return ReproductionChild(
        child_run_id=row["child_run_id"],
        round_id=row["round_id"],
        finding_id=row["finding_id"],
        ordinal=row["ordinal"],
        created_at=row["created_at"],
    )


def insert_child(
    conn: sqlite3.Connection,
    child_run_id: str,
    round_id: str,
    finding_id: str,
    ordinal: int,
    timestamp: str,
) -> None:
    """Vínculo da run filha com rodada, finding e tentativa, na transação que cria a run."""
    finding = select_finding(conn, finding_id)
    if finding.run_id == child_run_id:
        raise ReportingError(INVALID_REFERENCE, "A reproduction must run in a separate run.")
    conn.execute(
        "INSERT INTO reproduction_runs (child_run_id, round_id, finding_id, ordinal, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (child_run_id, round_id, finding_id, ordinal, timestamp),
    )


def select_child(conn: sqlite3.Connection, child_run_id: str) -> ReproductionChild | None:
    row = conn.execute(
        "SELECT * FROM reproduction_runs WHERE child_run_id = ?", (child_run_id,)
    ).fetchone()
    return _child_from_row(row) if row is not None else None


def select_children(conn: sqlite3.Connection, finding_id: str) -> list[ReproductionChild]:
    rows = conn.execute(
        "SELECT * FROM reproduction_runs WHERE finding_id = ? ORDER BY ordinal", (finding_id,)
    ).fetchall()
    return [_child_from_row(row) for row in rows]


def _report_from_row(row: sqlite3.Row) -> ReproducerReport:
    return ReproducerReport(
        child_run_id=row["child_run_id"],
        result=ReproducerResult(row["result"]),
        evidence_artifact_ids=tuple(
            str(item) for item in cast(list[JsonValue], json.loads(row["evidence_json"]))
        ),
        precondition_index=row["precondition_index"],
        note=row["note"],
        created_at=row["created_at"],
    )


def select_report(conn: sqlite3.Connection, child_run_id: str) -> ReproducerReport | None:
    row = conn.execute(
        "SELECT * FROM reproduction_reports WHERE child_run_id = ?", (child_run_id,)
    ).fetchone()
    return _report_from_row(row) if row is not None else None


def insert_report(
    conn: sqlite3.Connection,
    child_run_id: str,
    result: ReproducerResult,
    evidence_artifact_ids: tuple[str, ...],
    precondition_index: int | None,
    note: str,
    timestamp: str,
) -> ReproducerReport:
    """Relato único da tentativa; evidência e índice validados contra a run filha e o brief."""
    child = select_child(conn, child_run_id)
    if child is None:
        raise ReportingError(NOT_A_REPRODUCTION_RUN, "This run is not a reproduction attempt.")
    require_running(conn, child_run_id)
    if select_report(conn, child_run_id) is not None:
        raise ReportingError(ALREADY_REPORTED, "This attempt already reported its result.")
    if len(set(evidence_artifact_ids)) != len(evidence_artifact_ids):
        raise ReportingError(INVALID_EVIDENCE, "Each evidence reference must be unique.")
    run_artifacts(conn, child_run_id, evidence_artifact_ids)
    if result == ReproducerResult.REPRODUCED and not evidence_artifact_ids:
        raise ReportingError(
            INVALID_EVIDENCE, "A reproduced result needs evidence captured in this run."
        )
    preconditions = select_finding(conn, child.finding_id).preconditions
    if result == ReproducerResult.PRECONDITION_MISSING:
        if precondition_index is None or not 0 <= precondition_index < len(preconditions):
            raise ReportingError(
                INVALID_PRECONDITION_INDEX,
                f"precondition_index must be between 0 and {len(preconditions) - 1}.",
            )
    elif precondition_index is not None:
        raise ReportingError(
            INVALID_PRECONDITION_INDEX,
            "precondition_index is only accepted with precondition_missing.",
        )
    safe_note = redact_text(note)[:MAX_REPRODUCTION_NOTE_LENGTH]
    conn.execute(
        "INSERT INTO reproduction_reports (child_run_id, result, evidence_json, "
        "precondition_index, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            child_run_id,
            result.value,
            json.dumps(list(evidence_artifact_ids)),
            precondition_index,
            safe_note,
            timestamp,
        ),
    )
    report = select_report(conn, child_run_id)
    assert report is not None
    return report


def insert_attempt_detail(
    conn: sqlite3.Connection, detail: AttemptDetail, timestamp: str
) -> ReproductionAttempt:
    """Tentativa interpretada; repetir o mesmo ordinal devolve a linha já gravada."""
    existing = conn.execute(
        "SELECT * FROM finding_reproduction_attempts WHERE finding_id = ? AND ordinal = ? "
        "AND child_run_id = ?",
        (detail.finding_id, detail.ordinal, detail.child_run_id),
    ).fetchone()
    if existing is not None:
        return attempt_from_row(existing)
    finding = select_finding(conn, detail.finding_id)
    child = select_child(conn, detail.child_run_id)
    if child is None or child.finding_id != finding.id or child.ordinal != detail.ordinal:
        raise ReportingError(INVALID_REFERENCE, "The attempt must use its linked child run.")
    run_artifacts(conn, detail.child_run_id, detail.evidence_artifact_ids)
    if detail.outcome == ReproductionOutcome.REPRODUCED and not detail.evidence_artifact_ids:
        raise ReportingError(INVALID_EVIDENCE, "A reproduced attempt needs child-run evidence.")
    if (detail.outcome == ReproductionOutcome.INCONCLUSIVE) != (detail.reason is not None):
        raise ReportingError(INVALID_REFERENCE, "Only an inconclusive attempt carries a reason.")
    attempt_id = "repro_" + uuid4().hex[:16]
    executor = redact_text(detail.executor)
    conn.execute(
        "INSERT INTO finding_reproduction_attempts (id, finding_id, child_run_id, outcome, "
        "evidence_json, executor, attempted_at, ordinal, reason, note, precondition_index, "
        "verifier_executor) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            attempt_id,
            detail.finding_id,
            detail.child_run_id,
            detail.outcome.value,
            json.dumps(list(detail.evidence_artifact_ids)),
            executor,
            timestamp,
            detail.ordinal,
            detail.reason.value if detail.reason else None,
            redact_text(detail.note)[:MAX_REPRODUCTION_NOTE_LENGTH],
            detail.precondition_index,
            executor,
        ),
    )
    row = conn.execute(
        "SELECT * FROM finding_reproduction_attempts WHERE id = ?", (attempt_id,)
    ).fetchone()
    return attempt_from_row(row)


def append_reproduction_event(
    conn: sqlite3.Connection,
    run_id: str,
    event_type: EventType,
    payload: JsonObject,
    timestamp: str,
) -> None:
    """Escrita pós-terminal restrita aos três tipos de reprodução, na run explorada."""
    if event_type not in REPRODUCTION_EVENTS:
        raise InvalidEventError(f"{event_type.value} is not a reproduction event")
    if not select_run(conn, run_id).status.is_terminal:
        raise InvalidEventError("reproduction events are appended only after the run ended")
    insert_event(conn, run_id, prepare_event(event_type, payload=payload), timestamp)


def _verdict_from_row(row: sqlite3.Row) -> ReproductionVerdict:
    reason = row["reason"]
    return ReproductionVerdict(
        finding_id=row["finding_id"],
        run_id=row["run_id"],
        round_id=row["round_id"],
        attempts_planned=row["attempts_planned"],
        attempts_decisive=row["attempts_decisive"],
        reproduced_count=row["reproduced_count"],
        classification=FindingClassification(row["classification"]),
        reason=VerdictReason(reason) if reason else None,
        missing_precondition=row["missing_precondition"],
        intermittent=bool(row["intermittent"]),
        cross_executor=bool(row["cross_executor"]),
        explorer_executor=row["explorer_executor"],
        verifier_executor=row["verifier_executor"],
        notes=tuple(str(item) for item in cast(list[JsonValue], json.loads(row["notes_json"]))),
        decided_at=row["decided_at"],
        content_sha256=row["content_sha256"],
    )


_VERDICT_SELECT = (
    "SELECT v.*, f.run_id AS run_id FROM finding_reproduction_verdicts v "
    "JOIN findings f ON f.id = v.finding_id"
)


def select_verdict(conn: sqlite3.Connection, finding_id: str) -> ReproductionVerdict | None:
    row = conn.execute(f"{_VERDICT_SELECT} WHERE v.finding_id = ?", (finding_id,)).fetchone()
    return _verdict_from_row(row) if row is not None else None


def select_verdicts(conn: sqlite3.Connection, run_id: str) -> list[ReproductionVerdict]:
    rows = conn.execute(
        f"{_VERDICT_SELECT} WHERE f.run_id = ? ORDER BY f.created_at, f.id", (run_id,)
    ).fetchall()
    return [_verdict_from_row(row) for row in rows]


def _verdict_content(data: VerdictInput) -> JsonObject:
    return {
        "finding_id": data.finding_id,
        "attempts_planned": data.attempts_planned,
        "attempts_decisive": data.attempts_decisive,
        "reproduced_count": data.reproduced_count,
        "classification": data.classification.value,
        "reason": data.reason.value if data.reason else None,
        "missing_precondition": data.missing_precondition,
        "intermittent": data.intermittent,
        "cross_executor": data.cross_executor,
        "explorer_executor": data.explorer_executor,
        "verifier_executor": data.verifier_executor,
        "notes": list[JsonValue](data.notes),
    }


def settle_verdict(
    conn: sqlite3.Connection, data: VerdictInput, timestamp: str
) -> ReproductionVerdict:
    """Grava o veredito, promove ou conclui o finding e registra o evento, numa transação."""
    if select_verdict(conn, data.finding_id) is not None:
        raise ReproductionError(
            REPRODUCTION_NOT_ELIGIBLE, f"Finding {data.finding_id} already has a verdict."
        )
    finding = select_finding(conn, data.finding_id)
    if finding.classification != FindingClassification.SUSPECTED:
        raise ReproductionError(
            REPRODUCTION_NOT_ELIGIBLE, f"Finding {data.finding_id} is not suspected."
        )
    if data.classification == FindingClassification.CONFIRMED:
        linked = {child.child_run_id for child in select_children(conn, finding.id)}
        if not any(
            attempt.outcome == ReproductionOutcome.REPRODUCED
            and attempt.evidence_artifact_ids
            and attempt.child_run_id in linked
            for attempt in finding.reproduction_attempts
        ):
            raise ReproductionError(
                REPRODUCTION_NOT_ELIGIBLE,
                "A confirmed verdict needs a reproduced attempt with child-run evidence.",
            )
    content = _verdict_content(data)
    notes = [redact_text(note) for note in data.notes]
    conn.execute(
        "INSERT INTO finding_reproduction_verdicts (finding_id, round_id, attempts_planned, "
        "attempts_decisive, reproduced_count, classification, reason, missing_precondition, "
        "intermittent, cross_executor, explorer_executor, verifier_executor, notes_json, "
        "decided_at, content_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            data.finding_id,
            data.round_id,
            data.attempts_planned,
            data.attempts_decisive,
            data.reproduced_count,
            data.classification.value,
            data.reason.value if data.reason else None,
            redact_text(data.missing_precondition) if data.missing_precondition else None,
            int(data.intermittent),
            int(data.cross_executor),
            data.explorer_executor,
            data.verifier_executor,
            json.dumps(notes, ensure_ascii=False),
            timestamp,
            sha256_hex(canonical_json(content)),
        ),
    )
    if data.classification == FindingClassification.CONFIRMED:
        promote(conn, data.finding_id, timestamp)
    else:
        settle_inconclusive(conn, data.finding_id, timestamp)
    append_reproduction_event(
        conn,
        finding.run_id,
        EventType.REPRODUCTION_VERDICT,
        {
            "finding_id": data.finding_id,
            "title": finding.title,
            "label": data.label,
            "classification": data.classification.value,
            "reason": data.reason.value if data.reason else None,
            "missing_precondition": data.missing_precondition,
            "reproduced": data.reproduced_count,
            "decisive": data.attempts_decisive,
            "planned": data.attempts_planned,
            "explorer_executor": data.explorer_executor,
            "verifier_executor": data.verifier_executor,
            "cross_executor": data.cross_executor,
        },
        timestamp,
    )
    verdict = select_verdict(conn, data.finding_id)
    assert verdict is not None
    return verdict


def eligible_findings(conn: sqlite3.Connection, run_id: str) -> list[str]:
    """Achados de sistema suspected e sem veredito, em ordem de criação.

    Falhas do harness (agente ou ferramenta) não descrevem defeito do alvo e não são reproduzidas.
    """
    decided = {
        row["finding_id"]
        for row in conn.execute(
            "SELECT v.finding_id FROM finding_reproduction_verdicts v "
            "JOIN findings f ON f.id = v.finding_id WHERE f.run_id = ?",
            (run_id,),
        )
    }
    return [
        finding.id
        for finding in select_findings(conn, run_id)
        if finding.classification == FindingClassification.SUSPECTED
        and finding.failure_domain == FailureDomain.SYSTEM
        and finding.id not in decided
    ]


def select_status(conn: sqlite3.Connection, run_id: str) -> ReproductionStatus:
    select_run(conn, run_id)
    closed = conn.execute(
        "SELECT MAX(closed_at) AS closed FROM reproduction_rounds "
        "WHERE run_id = ? AND status <> 'open'",
        (run_id,),
    ).fetchone()
    return ReproductionStatus(
        run_id=run_id,
        open_round=select_open_round(conn, run_id),
        eligible=len(eligible_findings(conn, run_id)),
        verdicts=len(select_verdicts(conn, run_id)),
        last_closed_at=closed["closed"] if closed is not None else None,
    )
