"""Snapshots de issue, contexto de mudança e fontes controladas, só dentro da fachada."""

import json
import sqlite3
from collections.abc import Mapping
from typing import cast

from lastro.change_context.models import (
    ChangeContextDraft,
    IssueCapture,
    SourceFile,
    VersionMatch,
    canonical,
    sha256_text,
)
from lastro.history.errors import MissionPlanError
from lastro.history.events import insert_event, prepare_event
from lastro.history.models import (
    ChangeContextRecord,
    EventType,
    IssueSnapshotRecord,
    JsonObject,
    SourceFileRecord,
)

CONTEXT_INTEGRITY = "context_integrity"


# Erros de contexto seguem o envelope estável das tools de missão.
ChangeContextError = MissionPlanError


def _snapshot_from_row(row: sqlite3.Row) -> IssueSnapshotRecord:
    return IssueSnapshotRecord(
        id=row["id"],
        project_id=row["project_id"],
        source_kind=row["source_kind"],
        repository=row["repository"],
        issue_number=row["issue_number"],
        source_identity=row["source_identity"],
        content_sha256=row["content_sha256"],
        payload=cast(JsonObject, json.loads(row["payload_json"])),
        captured_at=row["captured_at"],
    )


def upsert_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    capture: IssueCapture,
    new_id: str,
    timestamp: str,
) -> str:
    """Mesmo conteúdo sanitizado da mesma origem reutiliza o snapshot; o anterior nunca muda."""
    content = capture.content
    row = conn.execute(
        "SELECT id FROM issue_snapshots WHERE project_id = ? AND source_kind = ? "
        "AND source_identity = ? AND content_sha256 = ?",
        (project_id, content.source_kind.value, content.source_identity, content.content_sha256),
    ).fetchone()
    if row is not None:
        return str(row["id"])
    conn.execute(
        "INSERT INTO issue_snapshots (id, project_id, repository, issue_number, source_kind, "
        "source_identity, content_sha256, payload_json, captured_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            new_id,
            project_id,
            content.repository,
            content.number,
            content.source_kind.value,
            content.source_identity,
            content.content_sha256,
            canonical(capture.stored_payload()),
            timestamp,
        ),
    )
    return new_id


def select_snapshot(
    conn: sqlite3.Connection, snapshot_id: str, project_id: str | None = None
) -> IssueSnapshotRecord:
    row = conn.execute("SELECT * FROM issue_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
    if row is None or (project_id is not None and row["project_id"] != project_id):
        raise ChangeContextError(CONTEXT_INTEGRITY, "issue snapshot not found in this project")
    return _snapshot_from_row(row)


def check_snapshot_integrity(snapshot: IssueSnapshotRecord) -> None:
    """O hash é recomputado do payload sanitizado, sem os instantes locais de captura."""
    payload = {
        key: value
        for key, value in snapshot.payload.items()
        if key not in ("capture_started_at", "capture_finished_at", "scan_level")
    }
    if sha256_text(canonical(payload)) != snapshot.content_sha256:
        raise ChangeContextError(CONTEXT_INTEGRITY, "issue snapshot hash mismatch")


def insert_context(
    conn: sqlite3.Connection,
    *,
    context_id: str,
    run_id: str,
    issue_snapshot_id: str | None,
    draft: ChangeContextDraft | None,
    issue_capture: IssueCapture | None,
    manifest_artifact_id: str | None,
    source_artifacts: Mapping[str, str],
    timestamp: str,
) -> ChangeContextRecord:
    payload: JsonObject
    if draft is not None:
        payload = draft.payload()
        state = draft.source_state.value
        flow_key = draft.flow_key
    else:
        payload = {"schema_version": 1, "source_state": "disabled"}
        state, flow_key = "disabled", None
    if issue_capture is not None:
        # A captura desta run é registrada mesmo quando o snapshot anterior é reutilizado.
        payload["issue_capture"] = {
            "started_at": issue_capture.capture_started_at,
            "finished_at": issue_capture.capture_finished_at,
            "scan_level": issue_capture.scan_level.value,
        }
    text = canonical(payload)
    conn.execute(
        "INSERT INTO change_contexts (id, run_id, issue_snapshot_id, source_state, flow_key, "
        "captured_at, content_sha256, payload_json, manifest_artifact_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            context_id,
            run_id,
            issue_snapshot_id,
            state,
            flow_key,
            timestamp,
            sha256_text(text),
            text,
            manifest_artifact_id,
        ),
    )
    for source in draft.sources if draft is not None else ():
        _insert_source(conn, context_id, source, source_artifacts[source.file_id])
    event_payload: JsonObject = {
        "context_id": context_id,
        "source_state": state,
        "comparison": payload.get("comparison"),
        "issue_snapshot_id": issue_snapshot_id,
        "files": len(draft.files) if draft else 0,
        "sources": len(draft.sources) if draft else 0,
        "excluded": len(draft.exclusions) if draft else 0,
        "truncated": draft.truncated if draft else False,
        "remote_freshness": payload.get("remote_freshness"),
    }
    insert_event(
        conn,
        run_id,
        prepare_event(EventType.CHANGE_CONTEXT_COLLECTED, payload=event_payload),
        timestamp,
    )
    for notice in draft.security if draft is not None else ():
        event = prepare_event(EventType.SECURITY_EVENT, payload=notice.as_json())
        insert_event(conn, run_id, event, timestamp)
    record = select_context(conn, run_id)
    assert record is not None
    return record


def _insert_source(
    conn: sqlite3.Connection, context_id: str, source: SourceFile, artifact_id: str
) -> None:
    conn.execute(
        "INSERT INTO source_files (id, context_id, relative_path, origin, revision_oid, "
        "content_sha256, size_bytes, artifact_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source.file_id,
            context_id,
            source.relative_path,
            source.origin.value,
            source.revision_oid,
            source.content_sha256,
            source.size_bytes,
            artifact_id,
        ),
    )


def _source_from_row(row: sqlite3.Row) -> SourceFileRecord:
    return SourceFileRecord(
        id=row["id"],
        context_id=row["context_id"],
        relative_path=row["relative_path"],
        origin=row["origin"],
        revision_oid=row["revision_oid"],
        content_sha256=row["content_sha256"],
        size_bytes=row["size_bytes"],
        artifact_id=row["artifact_id"],
    )


def select_context(conn: sqlite3.Connection, run_id: str) -> ChangeContextRecord | None:
    row = conn.execute("SELECT * FROM change_contexts WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    if sha256_text(row["payload_json"]) != row["content_sha256"]:
        raise ChangeContextError(CONTEXT_INTEGRITY, "change context hash mismatch")
    sources = conn.execute(
        "SELECT * FROM source_files WHERE context_id = ? ORDER BY relative_path, origin",
        (row["id"],),
    ).fetchall()
    return ChangeContextRecord(
        id=row["id"],
        run_id=row["run_id"],
        issue_snapshot_id=row["issue_snapshot_id"],
        source_state=row["source_state"],
        flow_key=row["flow_key"],
        captured_at=row["captured_at"],
        content_sha256=row["content_sha256"],
        payload=cast(JsonObject, json.loads(row["payload_json"])),
        manifest_artifact_id=row["manifest_artifact_id"],
        sources=tuple(_source_from_row(item) for item in sources),
    )


def select_version_observations(conn: sqlite3.Connection, run_id: str) -> list[JsonObject]:
    rows = conn.execute(
        "SELECT payload_json FROM events WHERE run_id = ? AND event_type = 'version_observed' "
        "ORDER BY sequence",
        (run_id,),
    ).fetchall()
    return [cast(JsonObject, json.loads(row["payload_json"])) for row in rows]


def record_version(
    conn: sqlite3.Connection, run_id: str, observation: JsonObject, timestamp: str
) -> None:
    """Grava a versão executada enquanto a run é ativa; o trigger protege as terminais."""
    executed = observation.get("executed_system_version")
    conn.execute(
        "UPDATE runs SET executed_system_version = ? WHERE id = ?",
        (executed if isinstance(executed, str) else None, run_id),
    )
    insert_event(
        conn, run_id, prepare_event(EventType.VERSION_OBSERVED, payload=observation), timestamp
    )


def _checks(conn: sqlite3.Connection, run_id: str) -> tuple[int, int]:
    rows = conn.execute(
        "SELECT json_extract(payload_json, '$.check.outcome') AS outcome FROM events "
        "WHERE run_id = ? AND event_type = 'policy_decision' AND tool_name = 'record_test_check' "
        "AND policy_decision = 'allow'",
        (run_id,),
    ).fetchall()
    outcomes = [row["outcome"] for row in rows]
    return outcomes.count("passed"), outcomes.count("failed")


def last_passed_baseline(
    conn: sqlite3.Connection, project_id: str, profile: str, tenant: str, flow_key: str
) -> tuple[str, str] | None:
    """Último commit comprovadamente executado numa run completed do mesmo escopo e fluxo.

    Só vale a run cuja última observação de versão é `matched` e cujos checks passaram sem
    falhas; nunca há substituição pela main ou por outra run.
    """
    rows = conn.execute(
        "SELECT runs.id, runs.analyzed_code_version FROM runs "
        "JOIN change_contexts c ON c.run_id = runs.id "
        "WHERE runs.project_id = ? AND runs.profile = ? AND runs.tenant = ? "
        "AND runs.status = 'completed' AND c.flow_key = ? "
        "AND runs.analyzed_code_version IS NOT NULL "
        "ORDER BY runs.started_at DESC",
        (project_id, profile, tenant, flow_key),
    ).fetchall()
    for row in rows:
        observations = select_version_observations(conn, row["id"])
        if not observations or observations[-1].get("version_match") != VersionMatch.MATCHED:
            continue
        passed, failed = _checks(conn, row["id"])
        if passed > 0 and failed == 0:
            return str(row["id"]), str(row["analyzed_code_version"])
    return None
