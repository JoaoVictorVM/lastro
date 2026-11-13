"""Fila de exportação derivada do histórico: contexto congelado, projeção, lease e ack.

Toda função roda dentro de uma transação curta da fachada; nenhuma faz rede. O cursor
`projected_sequence` avança na mesma transação que grava os descritores, e o ack só aceita
linhas `inflight` do token de lease vigente.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote

from lastro.config.observability import (
    MAPPING_VERSION,
    Destination,
    EvaluationTraceContext,
    ObservabilityContextInput,
)
from lastro.history.database import db_timestamp
from lastro.history.events import event_from_row, select_events
from lastro.history.models import EventRecord, EventType, PolicyDecision
from lastro.history.native import select_calls
from lastro.history.redaction import sha256_hex
from lastro.history.runs import select_run
from lastro.observability.models import ExportStatus, ObservabilityStatus, SpanDescriptor

if TYPE_CHECKING:
    from lastro.observability.projection import RunProjection

BATCH_SIZE = 128
LEASE_TTL_SECONDS = 15.0
BACKOFF_SECONDS = (1, 2, 4, 8, 16, 32, 60)
type FailureKind = Literal["retry", "blocked", "rejected"]


class ObservabilityStateError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ObservabilityRun:
    run_id: str
    trace_id: str
    root_event_id: int
    root_span_id: str
    mapping_version: int
    destination: Destination
    destination_sha256: str
    evaluation: EvaluationTraceContext | None
    projected_sequence: int
    export_status: ExportStatus
    lease_token: str | None
    lease_expires_at: str | None
    last_error_code: str | None

    @property
    def trace_url(self) -> str:
        return trace_url(self.destination, self.trace_id)


@dataclass(frozen=True)
class BatchItem:
    event_id: int
    descriptor: SpanDescriptor


def trace_url(destination: Destination, trace_id: str) -> str:
    base = destination.ui_url.rstrip("/")
    project = quote(destination.langfuse_project_id, safe="")
    return f"{base}/project/{project}/traces/{quote(trace_id, safe='')}"


def _context_from_row(row: sqlite3.Row) -> ObservabilityRun:
    return ObservabilityRun(
        run_id=row["run_id"],
        trace_id=row["trace_id"],
        root_event_id=int(row["root_event_id"]),
        root_span_id=row["root_span_id"],
        mapping_version=int(row["mapping_version"]),
        destination=Destination(
            endpoint=row["endpoint"],
            ui_url=row["ui_url"],
            langfuse_project_id=row["langfuse_project_id"],
        ),
        destination_sha256=row["destination_sha256"],
        evaluation=EvaluationTraceContext.from_columns(row["suite_id"], row["condition_name"]),
        projected_sequence=int(row["projected_sequence"]),
        export_status=row["export_status"],
        lease_token=row["lease_token"],
        lease_expires_at=row["lease_expires_at"],
        last_error_code=row["last_error_code"],
    )


def select_context(conn: sqlite3.Connection, run_id: str) -> ObservabilityRun | None:
    row = conn.execute("SELECT * FROM observability_runs WHERE run_id = ?", (run_id,)).fetchone()
    return _context_from_row(row) if row is not None else None


def insert_context(
    conn: sqlite3.Connection,
    project_id: str,
    root: EventRecord,
    data: ObservabilityContextInput,
    timestamp: str,
) -> ObservabilityRun:
    """Congela destino e avaliação junto com run_started, na transação que cria a run."""
    # Tardio: a projeção importa o pacote do histórico, que importa este módulo.
    from lastro.observability.projection import span_id_for, trace_id_for

    if root.event_type != EventType.RUN_STARTED:
        raise ObservabilityStateError("obs_config_invalid", "root must be run_started")
    trace_id = trace_id_for(project_id, root.run_id)
    clash = conn.execute(
        "SELECT run_id FROM observability_runs WHERE trace_id = ?", (trace_id,)
    ).fetchone()
    if clash is not None and clash["run_id"] != root.run_id:
        raise ObservabilityStateError("obs_projection_failed", "trace id collision")
    root_span = span_id_for(project_id, root.run_id, root.id)
    evaluation = data.evaluation
    conn.execute(
        "INSERT INTO observability_runs (run_id, trace_id, root_event_id, root_span_id, "
        "mapping_version, endpoint, ui_url, langfuse_project_id, destination_sha256, suite_id, "
        "condition_name, projected_sequence, export_status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'pending', ?, ?)",
        (
            root.run_id,
            trace_id,
            root.id,
            root_span,
            MAPPING_VERSION,
            data.destination.endpoint,
            data.destination.ui_url,
            data.destination.langfuse_project_id,
            data.destination.sha256,
            evaluation.suite_id if evaluation else None,
            evaluation.condition if evaluation else None,
            timestamp,
            timestamp,
        ),
    )
    conn.execute(
        "INSERT INTO observability_spans (event_id, run_id, event_sequence, span_id, state) "
        "VALUES (?, ?, ?, ?, 'waiting_end')",
        (root.id, root.run_id, root.sequence, root_span),
    )
    conn.execute(
        "UPDATE observability_runs SET projected_sequence = ? WHERE run_id = ?",
        (root.sequence, root.run_id),
    )
    context = select_context(conn, root.run_id)
    assert context is not None
    return context


def _origin_run(conn: sqlite3.Connection, run_id: str) -> str | None:
    row = conn.execute(
        "SELECT parent_run_id FROM run_links WHERE child_run_id = ?", (run_id,)
    ).fetchone()
    if row is not None:
        return str(row["parent_run_id"])
    row = conn.execute(
        "SELECT f.run_id AS origin FROM reproduction_runs c "
        "JOIN findings f ON f.id = c.finding_id WHERE c.child_run_id = ?",
        (run_id,),
    ).fetchone()
    return str(row["origin"]) if row is not None else None


def origin_evaluation(conn: sqlite3.Connection, run_id: str) -> EvaluationTraceContext | None:
    """Etiquetas herdadas por retomada e reprodução, lidas da run de origem."""
    origin = _origin_run(conn, run_id)
    if origin is None:
        return None
    context = select_context(conn, origin)
    return context.evaluation if context is not None else None


def _memory_outcome(conn: sqlite3.Connection, run_id: str) -> str | None:
    row = conn.execute(
        "SELECT outcome FROM run_memory_outcomes WHERE run_id = ?", (run_id,)
    ).fetchone()
    return str(row["outcome"]) if row is not None else None


def _first_by_payload(
    conn: sqlite3.Connection, run_id: str, event_type: EventType, key: str, *, latest: bool
) -> dict[str, str]:
    order = "DESC" if latest else "ASC"
    rows = conn.execute(
        f"SELECT timestamp, payload_json FROM events WHERE run_id = ? AND event_type = ? "
        f"ORDER BY sequence {order}",
        (run_id, event_type.value),
    ).fetchall()
    found: dict[str, str] = {}
    for row in rows:
        payload = json.loads(row["payload_json"] or "null")
        if not isinstance(payload, dict):
            continue
        values = payload.get(key)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        items = values if isinstance(values, list) else [values]  # pyright: ignore[reportUnknownVariableType]
        for item in items:  # pyright: ignore[reportUnknownVariableType]
            if isinstance(item, str) and item not in found:
                found[item] = str(row["timestamp"])
    return found


def _decisions(conn: sqlite3.Connection, run_id: str) -> dict[str, PolicyDecision]:
    rows = conn.execute(
        "SELECT correlation_id, policy_decision FROM events WHERE run_id = ? "
        "AND event_type = 'policy_decision' AND correlation_id IS NOT NULL ORDER BY sequence",
        (run_id,),
    ).fetchall()
    return {str(row["correlation_id"]): PolicyDecision(row["policy_decision"]) for row in rows}


def projection_input(
    conn: sqlite3.Connection, context: ObservabilityRun
) -> "tuple[RunProjection, bool]":
    from lastro.observability.projection import RunProjection

    run = select_run(conn, context.run_id)
    evaluation = context.evaluation
    projection = RunProjection(
        project_id=run.project_id,
        run_id=run.id,
        executor=run.executor_name,
        started_at=run.started_at,
        ended_at=run.ended_at,
        lastro_version=run.lastro_version,
        root_event_id=context.root_event_id,
        origin_run_id=_origin_run(conn, run.id),
        suite_id=evaluation.suite_id if evaluation else None,
        condition=evaluation.condition if evaluation else None,
        memory_outcome=_memory_outcome(conn, run.id),
        model_calls=tuple(select_calls(conn, run.id)),
        decisions=_decisions(conn, run.id),
        approval_requests=_first_by_payload(
            conn, run.id, EventType.APPROVAL_REQUESTED, "action_id", latest=False
        ),
        reproduction_starts=_first_by_payload(
            conn, run.id, EventType.REPRODUCTION_STARTED, "finding_ids", latest=True
        ),
    )
    return projection, run.status.is_terminal


def _awaiting_decision(event: EventRecord, decisions: dict[str, PolicyDecision]) -> bool:
    return (
        event.event_type == EventType.TOOL_CALL
        and event.correlation_id is not None
        and event.correlation_id not in decisions
    )


def _insert_span(conn: sqlite3.Connection, event: EventRecord, descriptor: SpanDescriptor) -> None:
    from lastro.observability.projection import ProjectionError

    canonical = descriptor.canonical()
    existing = conn.execute(
        "SELECT span_id FROM observability_spans WHERE run_id = ? AND span_id = ? "
        "AND event_id <> ?",
        (event.run_id, descriptor.span_id, event.id),
    ).fetchone()
    if existing is not None:
        raise ProjectionError("span id collision")
    conn.execute(
        "INSERT INTO observability_spans (event_id, run_id, event_sequence, span_id, "
        "descriptor_json, descriptor_sha256, state) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
        (
            event.id,
            event.run_id,
            event.sequence,
            descriptor.span_id,
            canonical,
            sha256_hex(canonical),
        ),
    )


def project(conn: sqlite3.Connection, run_id: str, now: str, limit: int = BATCH_SIZE) -> int:
    """Materializa até `limit` eventos novos e, na run terminal, a raiz; retorna quantos."""
    from lastro.observability.projection import describe

    context = select_context(conn, run_id)
    if context is None:
        return 0
    projection, terminal = projection_input(conn, context)
    events = select_events(conn, run_id, after_sequence=context.projected_sequence, limit=limit)
    decisions = dict(projection.decisions)
    cursor = context.projected_sequence
    count = 0
    for event in events:
        # Sem a decisão correlata, o descritor imutável não poderia distinguir negação.
        if not terminal and _awaiting_decision(event, decisions):
            break
        if event.id != context.root_event_id:
            _insert_span(conn, event, describe(event, projection))
            count += 1
        cursor = event.sequence
    if cursor != context.projected_sequence:
        conn.execute(
            "UPDATE observability_runs SET projected_sequence = ?, updated_at = ? WHERE run_id = ?",
            (cursor, now, run_id),
        )
    if terminal:
        row = conn.execute("SELECT * FROM events WHERE id = ?", (context.root_event_id,)).fetchone()
        root = event_from_row(row)
        descriptor = describe(root, projection)
        canonical = descriptor.canonical()
        updated = conn.execute(
            "UPDATE observability_spans SET descriptor_json = ?, descriptor_sha256 = ?, "
            "state = 'pending' WHERE event_id = ? AND state = 'waiting_end'",
            (canonical, sha256_hex(canonical), root.id),
        ).rowcount
        count += updated
    return count


def _expired(expires_at: str | None, now: str) -> bool:
    return expires_at is None or expires_at <= now


def claim_lease(
    conn: sqlite3.Connection, run_id: str, token: str, now: datetime, ttl: float = LEASE_TTL_SECONDS
) -> bool:
    """Compare-and-set: livre, expirado ou já do mesmo token; recupera inflight órfão."""
    context = select_context(conn, run_id)
    if context is None:
        return False
    stamp = db_timestamp(now)
    if context.lease_token not in (None, token) and not _expired(context.lease_expires_at, stamp):
        return False
    conn.execute(
        "UPDATE observability_runs SET lease_token = ?, lease_expires_at = ?, updated_at = ? "
        "WHERE run_id = ?",
        (token, db_timestamp(now + timedelta(seconds=ttl)), stamp, run_id),
    )
    conn.execute(
        "UPDATE observability_spans SET state = 'pending', inflight_token = NULL "
        "WHERE run_id = ? AND state = 'inflight' AND inflight_token <> ?",
        (run_id, token),
    )
    return True


def holds_lease(conn: sqlite3.Connection, run_id: str, token: str, now: str) -> bool:
    row = conn.execute(
        "SELECT lease_token, lease_expires_at FROM observability_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return (
        row is not None
        and row["lease_token"] == token
        and not _expired(row["lease_expires_at"], now)
    )


def release_lease(conn: sqlite3.Connection, run_id: str, token: str, now: str) -> None:
    conn.execute(
        "UPDATE observability_spans SET state = 'pending', inflight_token = NULL "
        "WHERE run_id = ? AND state = 'inflight' AND inflight_token = ?",
        (run_id, token),
    )
    conn.execute(
        "UPDATE observability_runs SET lease_token = NULL, lease_expires_at = NULL, "
        "updated_at = ? WHERE run_id = ? AND lease_token = ?",
        (now, run_id, token),
    )


def take_batch(
    conn: sqlite3.Connection, run_id: str, token: str, now: str, limit: int = BATCH_SIZE
) -> list[BatchItem]:
    if not holds_lease(conn, run_id, token, now):
        return []
    rows = conn.execute(
        "SELECT event_id, descriptor_json FROM observability_spans WHERE run_id = ? "
        "AND state = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
        "ORDER BY event_sequence LIMIT ?",
        (run_id, now, limit),
    ).fetchall()
    items = [
        BatchItem(int(row["event_id"]), SpanDescriptor.from_json(row["descriptor_json"]))
        for row in rows
    ]
    for item in items:
        conn.execute(
            "UPDATE observability_spans SET state = 'inflight', inflight_token = ?, "
            "attempts = attempts + 1 WHERE event_id = ?",
            (token, item.event_id),
        )
    return items


def acknowledge(
    conn: sqlite3.Connection, run_id: str, token: str, event_ids: list[int], now: str
) -> int:
    """Ack só com o lease vigente; quem perdeu o lease não confirma nada do novo dono."""
    if not holds_lease(conn, run_id, token, now):
        return 0
    done = 0
    for event_id in event_ids:
        done += conn.execute(
            "UPDATE observability_spans SET state = 'acknowledged', acknowledged_at = ?, "
            "inflight_token = NULL, next_attempt_at = NULL "
            "WHERE event_id = ? AND run_id = ? AND state = 'inflight' AND inflight_token = ?",
            (now, event_id, run_id, token),
        ).rowcount
    remaining = _count(conn, run_id, ("pending", "inflight"))
    conn.execute(
        "UPDATE observability_runs SET export_status = ?, last_error_code = NULL, "
        "updated_at = ? WHERE run_id = ? AND export_status <> 'suspended'",
        ("pending" if remaining else "idle", now, run_id),
    )
    return done


def backoff_seconds(attempts: int) -> int:
    index = max(attempts, 1) - 1
    return BACKOFF_SECONDS[min(index, len(BACKOFF_SECONDS) - 1)]


def record_failure(
    conn: sqlite3.Connection,
    run_id: str,
    token: str,
    event_ids: list[int],
    kind: FailureKind,
    code: str,
    now: datetime,
    retry_after: float | None = None,
) -> None:
    stamp = db_timestamp(now)
    for event_id in event_ids:
        row = conn.execute(
            "SELECT attempts FROM observability_spans WHERE event_id = ? AND run_id = ? "
            "AND state = 'inflight' AND inflight_token = ?",
            (event_id, run_id, token),
        ).fetchone()
        if row is None:
            continue
        if kind == "rejected":
            conn.execute(
                "UPDATE observability_spans SET state = 'rejected', inflight_token = NULL, "
                "next_attempt_at = NULL WHERE event_id = ?",
                (event_id,),
            )
            continue
        delay = float(backoff_seconds(int(row["attempts"])))
        if retry_after is not None:
            delay = max(delay, retry_after)
        conn.execute(
            "UPDATE observability_spans SET state = 'pending', inflight_token = NULL, "
            "next_attempt_at = ? WHERE event_id = ?",
            (db_timestamp(now + timedelta(seconds=delay)), event_id),
        )
    status = "pending" if kind == "retry" else "blocked"
    set_status(conn, run_id, status, code, stamp)


def set_status(
    conn: sqlite3.Connection, run_id: str, status: ExportStatus, code: str | None, now: str
) -> None:
    conn.execute(
        "UPDATE observability_runs SET export_status = ?, last_error_code = ?, updated_at = ? "
        "WHERE run_id = ?",
        (status, code, now, run_id),
    )


def unblock(conn: sqlite3.Connection, run_id: str, now: str) -> None:
    """Recovery explícito libera lotes ainda pendentes; rejeitados nunca voltam à fila."""
    conn.execute(
        "UPDATE observability_spans SET next_attempt_at = NULL WHERE run_id = ? "
        "AND state = 'pending'",
        (run_id,),
    )
    conn.execute(
        "UPDATE observability_runs SET export_status = 'pending', updated_at = ? "
        "WHERE run_id = ? AND export_status IN ('blocked', 'suspended')",
        (now, run_id),
    )


def _count(conn: sqlite3.Connection, run_id: str, states: tuple[str, ...]) -> int:
    placeholders = ", ".join("?" for _ in states)
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM observability_spans WHERE run_id = ? "
        f"AND state IN ({placeholders})",
        (run_id, *states),
    ).fetchone()
    return int(row["n"])


def status(conn: sqlite3.Connection, run_id: str) -> ObservabilityStatus | None:
    context = select_context(conn, run_id)
    if context is None:
        return None
    evaluation = context.evaluation
    return ObservabilityStatus(
        run_id=run_id,
        trace_id=context.trace_id,
        trace_url=context.trace_url,
        export_status=context.export_status,
        projected_events=_count(
            conn, run_id, ("waiting_end", "pending", "inflight", "acknowledged", "rejected")
        ),
        acknowledged_spans=_count(conn, run_id, ("acknowledged",)),
        pending_spans=_count(conn, run_id, ("waiting_end", "pending", "inflight")),
        waiting_end=_count(conn, run_id, ("waiting_end",)),
        rejected_spans=_count(conn, run_id, ("rejected",)),
        last_error_code=context.last_error_code,
        suite_id=evaluation.suite_id if evaluation else None,
        condition=evaluation.condition if evaluation else None,
    )


def list_span_rows(conn: sqlite3.Connection, run_id: str) -> list[tuple[int, str, str | None]]:
    """(event_id, estado, descritor) em ordem de sequência, para auditoria e testes."""
    rows = conn.execute(
        "SELECT event_id, state, descriptor_json FROM observability_spans WHERE run_id = ? "
        "ORDER BY event_sequence",
        (run_id,),
    ).fetchall()
    return [(int(row["event_id"]), str(row["state"]), row["descriptor_json"]) for row in rows]
