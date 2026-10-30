"""Consultas do histórico: filtros combinados por AND, agregados e cursor de eventos."""

import re
import sqlite3

from lastro.history.database import db_timestamp
from lastro.history.models import FindingCounts, RunFilter, RunSummary
from lastro.history.runs import run_from_row

_ISSUE_NUMBER = re.compile(r"^#?(\d+)$")


def _where(run_filter: RunFilter) -> tuple[str, list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if run_filter.status is not None:
        clauses.append("runs.status = ?")
        params.append(run_filter.status.value)
    if run_filter.profile is not None:
        clauses.append("runs.profile = ?")
        params.append(run_filter.profile)
    if run_filter.since is not None:
        clauses.append("runs.started_at >= ?")
        params.append(db_timestamp(run_filter.since))
    if run_filter.flow is not None:
        clauses.append(
            "EXISTS (SELECT 1 FROM criteria c WHERE c.run_id = runs.id "
            "AND json_extract(c.payload_json, '$.flow') = ?)"
        )
        params.append(run_filter.flow)
    if run_filter.issue is not None:
        number = _ISSUE_NUMBER.match(run_filter.issue)
        clauses.append(
            "(runs.issue_snapshot_id = ? OR EXISTS (SELECT 1 FROM issue_snapshots s "
            "WHERE s.id = runs.issue_snapshot_id AND s.issue_number = ?))"
        )
        params.extend([run_filter.issue, int(number.group(1)) if number else None])
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


_SUMMARY_COLUMNS = """
    (SELECT COUNT(*) FROM findings f
        WHERE f.run_id = runs.id AND f.classification = 'confirmed') AS findings_confirmed,
    (SELECT COUNT(*) FROM findings f
        WHERE f.run_id = runs.id AND f.classification = 'suspected') AS findings_suspected,
    (SELECT COUNT(*) FROM findings f
        WHERE f.run_id = runs.id AND f.classification = 'inconclusive') AS findings_inconclusive,
    (SELECT COUNT(*) FROM created_records r
        WHERE r.run_id = runs.id AND r.cleanup_status = 'pending') AS pending_cleanup
"""


def _flows(conn: sqlite3.Connection, run_id: str) -> tuple[str, ...]:
    rows = conn.execute(
        "SELECT DISTINCT json_extract(payload_json, '$.flow') AS flow FROM criteria "
        "WHERE run_id = ? AND json_type(payload_json, '$.flow') = 'text' ORDER BY flow",
        (run_id,),
    ).fetchall()
    return tuple(str(row["flow"]) for row in rows)


def select_runs(conn: sqlite3.Connection, run_filter: RunFilter) -> list[RunSummary]:
    if run_filter.limit < 1:
        raise ValueError("o limite do histórico precisa ser positivo")
    where, params = _where(run_filter)
    rows = conn.execute(
        f"SELECT runs.*, {_SUMMARY_COLUMNS} FROM runs{where} "
        "ORDER BY runs.started_at DESC, runs.id DESC LIMIT ?",
        (*params, run_filter.limit),
    ).fetchall()
    return [
        RunSummary(
            record=run_from_row(row),
            flows=_flows(conn, row["id"]),
            findings=FindingCounts(
                confirmed=row["findings_confirmed"],
                suspected=row["findings_suspected"],
                inconclusive=row["findings_inconclusive"],
            ),
            pending_cleanup=row["pending_cleanup"],
        )
        for row in rows
    ]
