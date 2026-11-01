"""Operações de sinais, exclusivamente dentro das transações da fachada."""

import json
import sqlite3
from uuid import uuid4

from lastro.history.errors import InvalidEventError, RunNotActiveError
from lastro.history.events import insert_event, prepare_event
from lastro.history.models import EventType
from lastro.history.redaction import redact_to_json
from lastro.history.runs import select_run
from lastro.oracles.models import OracleOccurrence, OracleSignal


def from_row(row: sqlite3.Row) -> OracleSignal:
    return OracleSignal(
        id=row["id"],
        run_id=row["run_id"],
        signal_type=row["signal_type"],
        url_path=row["url_path"],
        message=row["message"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        occurrence_count=row["occurrence_count"],
        first_occurrence=OracleOccurrence.model_validate_json(row["first_occurrence_json"]),
        last_occurrence=OracleOccurrence.model_validate_json(row["last_occurrence_json"]),
        last_event_id=row["last_event_id"],
    )


def select_signals(
    conn: sqlite3.Connection, run_id: str, limit: int | None = None
) -> list[OracleSignal]:
    select_run(conn, run_id)
    if limit is not None and (isinstance(limit, bool) or limit <= 0):
        raise ValueError("limit must be positive")
    query = "SELECT * FROM oracle_signals WHERE run_id = ? ORDER BY last_event_id DESC, id"
    params: tuple[object, ...] = (run_id,)
    if limit is not None:
        query += " LIMIT ?"
        params = (*params, limit)
    return [from_row(row) for row in conn.execute(query, params)]


def record_signal(
    conn: sqlite3.Connection, run_id: str, occurrence: OracleOccurrence
) -> OracleSignal:
    run = select_run(conn, run_id)
    if run.status.is_terminal:
        raise RunNotActiveError(run_id, run.status.value)
    if occurrence.action_id is not None:
        action = conn.execute(
            "SELECT 1 FROM events WHERE run_id = ? AND event_type = 'tool_call' "
            "AND correlation_id = ? AND sequence = ?",
            (run_id, occurrence.action_id, occurrence.action_sequence),
        ).fetchone()
        if action is None:
            raise InvalidEventError("oracle action must belong to the same run")
    previous = conn.execute(
        "SELECT payload_json FROM events WHERE run_id = ? AND event_type = 'oracle_signal' "
        "AND json_extract(payload_json, '$.occurrence_id') = ?",
        (run_id, occurrence.occurrence_id),
    ).fetchone()
    if previous is not None:
        signal_id = json.loads(previous["payload_json"])["signal_id"]
        return from_row(
            conn.execute("SELECT * FROM oracle_signals WHERE id = ?", (signal_id,)).fetchone()
        )
    identity = [occurrence.signal_type, occurrence.url_path, occurrence.message]
    digest = redact_to_json(identity).sha256
    row = conn.execute(
        "SELECT * FROM oracle_signals WHERE run_id = ? AND dedupe_sha256 = ?", (run_id, digest)
    ).fetchone()
    if row is not None and [row["signal_type"], row["url_path"], row["message"]] != identity:
        raise InvalidEventError("oracle identity hash collision")
    signal_id = row["id"] if row is not None else f"oracle_{uuid4().hex}"
    count = row["occurrence_count"] + 1 if row is not None else 1
    snapshot = redact_to_json(occurrence.model_dump()).canonical
    payload = {
        **occurrence.model_dump(),
        "signal_id": signal_id,
        "run_id": run_id,
        "occurrence_count": count,
    }
    event = insert_event(
        conn, run_id, prepare_event(EventType.ORACLE_SIGNAL, payload=payload), occurrence.timestamp
    )
    if row is None:
        conn.execute(
            "INSERT INTO oracle_signals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                signal_id,
                run_id,
                occurrence.signal_type,
                occurrence.url_path,
                occurrence.message,
                digest,
                occurrence.timestamp,
                occurrence.timestamp,
                count,
                snapshot,
                snapshot,
                event.id,
            ),
        )
    else:
        conn.execute(
            "UPDATE oracle_signals SET last_seen_at = ?, occurrence_count = ?, "
            "last_occurrence_json = ?, last_event_id = ? WHERE id = ?",
            (occurrence.timestamp, count, snapshot, event.id, signal_id),
        )
    return from_row(
        conn.execute("SELECT * FROM oracle_signals WHERE id = ?", (signal_id,)).fetchone()
    )
