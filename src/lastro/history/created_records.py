"""Registros criados pelas runs, exclusivamente dentro das transações da fachada."""

import json
import sqlite3
from typing import Literal, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from lastro.history.errors import (
    CleanupTransitionError,
    CreatedRecordNotFoundError,
    InvalidEventError,
    RunNotActiveError,
)
from lastro.history.events import PreparedEvent, insert_event, prepare_event
from lastro.history.models import (
    UNKNOWN_IDENTIFIER,
    CleanupResult,
    CleanupStatus,
    CreatedRecord,
    CreatedRecordInput,
    EventType,
)
from lastro.history.redaction import redact_text, redact_to_json
from lastro.history.runs import select_run

PAYLOAD_VERSION = 1
NOT_EXTRACTED = "identifier_not_extracted"
TENANT_MISMATCH = "tenant_mismatch"
COLLISION = "identifier_collision"
UNEXPECTED_STATUS = "unexpected_success_status"

_SELECT = (
    "SELECT r.*, runs.profile AS run_profile FROM created_records r "
    "JOIN runs ON runs.id = r.run_id WHERE runs.project_id = ?"
)


class RecordPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1] = PAYLOAD_VERSION
    action_id: str
    event_id: int
    identified: bool
    expected_tenant: str
    detail_path: str | None = None
    cleanup_instructions: str | None = None
    warnings: list[str]
    unexpected_success_status: int | None = None


def from_row(row: sqlite3.Row) -> CreatedRecord:
    data: dict[str, object] = json.loads(row["payload_json"])
    action = data.get("action_id")
    expected = data.get("expected_tenant")
    detail = data.get("detail_path")
    instructions = data.get("cleanup_instructions")
    status = data.get("unexpected_success_status")
    warnings = data.get("warnings")
    items = cast(list[object], warnings) if isinstance(warnings, list) else []
    return CreatedRecord(
        id=row["id"],
        run_id=row["run_id"],
        profile=row["run_profile"],
        entity_type=row["entity_type"],
        system_identifier=row["system_identifier"],
        tenant=row["tenant"],
        cleanup_status=CleanupStatus(row["cleanup_status"]),
        created_at=row["created_at"],
        cleaned_at=row["cleaned_at"],
        action_id=action if isinstance(action, str) else None,
        identified=data.get("identified") is True,
        expected_tenant=expected if isinstance(expected, str) else None,
        detail_path=detail if isinstance(detail, str) else None,
        cleanup_instructions=instructions if isinstance(instructions, str) else None,
        warnings=tuple(item for item in items if isinstance(item, str)),
        unexpected_success_status=status
        if isinstance(status, int) and not isinstance(status, bool)
        else None,
    )


def _select_one(conn: sqlite3.Connection, project_id: str, tracking_id: str) -> CreatedRecord:
    row = conn.execute(_SELECT + " AND r.id = ?", (project_id, tracking_id)).fetchone()
    if row is None:
        raise CreatedRecordNotFoundError(tracking_id)
    return from_row(row)


def _require_text(value: str, field: str) -> str:
    if not value.strip():
        raise InvalidEventError(f"{field} não pode ser vazio")
    return value


def creation_event(
    data: CreatedRecordInput,
    tracking_id: str | None = None,
    status: CleanupStatus | None = None,
    detail_path: str | None = None,
    warnings: list[str] | None = None,
) -> PreparedEvent:
    identified = bool(data.system_identifier and data.system_identifier.strip())
    return prepare_event(
        EventType.RECORD_CREATED,
        payload={
            "tracking_id": tracking_id,
            "run_id": data.run_id,
            "tenant": data.tenant,
            "entity": data.entity,
            "record_id": data.system_identifier if identified else UNKNOWN_IDENTIFIER,
            "identified": identified,
            "action_id": data.action_id,
            "cleanup_status": status.value if status else None,
            "detail_path": detail_path,
            "warnings": warnings or [],
        },
    )


def insert_created_record(
    conn: sqlite3.Connection, project_id: str, data: CreatedRecordInput, timestamp: str
) -> CreatedRecord:
    """Grava record_created e a linha juntos; a reentrega da mesma ação é idempotente."""
    run = select_run(conn, data.run_id)
    if run.status.is_terminal:
        raise RunNotActiveError(data.run_id, run.status.value)
    if run.project_id != project_id:
        raise InvalidEventError("run de outro projeto")
    for value, field in (
        (data.action_id, "action_id"),
        (data.entity, "entity"),
        (data.tenant, "tenant"),
        (data.expected_tenant, "expected_tenant"),
    ):
        _require_text(value, field)
    allowed = conn.execute(
        "SELECT 1 FROM events WHERE run_id = ? AND event_type = 'policy_decision' "
        "AND correlation_id = ? AND policy_decision = 'allow' AND EXISTS ("
        "SELECT 1 FROM events WHERE run_id = ? AND event_type = 'tool_call' "
        "AND correlation_id = ?)",
        (data.run_id, data.action_id, data.run_id, data.action_id),
    ).fetchone()
    if allowed is None:
        raise InvalidEventError("a criação exige uma ação autorizada da mesma run")
    identified = bool(data.system_identifier and data.system_identifier.strip())
    identifier = data.system_identifier if identified and data.system_identifier else None
    system_identifier = redact_text(identifier) if identifier else UNKNOWN_IDENTIFIER
    tenant = redact_text(data.tenant)
    previous = conn.execute(
        _SELECT + " AND r.run_id = ? AND json_extract(r.payload_json, '$.action_id') = ?",
        (project_id, data.run_id, data.action_id),
    ).fetchone()
    if previous is not None:
        existing = from_row(previous)
        if (
            existing.entity_type,
            existing.tenant,
            existing.system_identifier,
        ) != (data.entity, tenant, system_identifier):
            raise InvalidEventError("ambiguous_creation")
        return existing
    warnings: list[str] = []
    if not identified:
        warnings.append(NOT_EXTRACTED)
    if data.tenant != data.expected_tenant:
        warnings.append(TENANT_MISMATCH)
    if (
        identified
        and conn.execute(
            "SELECT 1 FROM created_records WHERE run_id = ? AND entity_type = ? AND tenant = ? "
            "AND system_identifier = ?",
            (data.run_id, data.entity, tenant, system_identifier),
        ).fetchone()
    ):
        warnings.append(COLLISION)
    if data.unexpected_success_status is not None:
        warnings.append(UNEXPECTED_STATUS)
    tracking_id = f"created_{uuid4().hex}"
    status = CleanupStatus.PENDING if data.cleanup_required else CleanupStatus.NOT_REQUIRED
    detail = data.detail_path if identified else None
    event = insert_event(
        conn, data.run_id, creation_event(data, tracking_id, status, detail, warnings), timestamp
    )
    payload = RecordPayload(
        action_id=data.action_id,
        event_id=event.id,
        identified=identified,
        expected_tenant=data.expected_tenant,
        detail_path=detail,
        cleanup_instructions=data.cleanup_instructions,
        warnings=warnings,
        unexpected_success_status=data.unexpected_success_status,
    )
    conn.execute(
        "INSERT INTO created_records (id, run_id, entity_type, system_identifier, tenant, "
        "cleanup_status, payload_json, created_at, cleaned_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        (
            tracking_id,
            data.run_id,
            data.entity,
            system_identifier,
            tenant,
            status.value,
            redact_to_json(payload.model_dump(mode="json")).canonical,
            timestamp,
        ),
    )
    return _select_one(conn, project_id, tracking_id)


def select_created_records(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    run_id: str | None = None,
    cleanup_status: CleanupStatus | None = None,
    profile: str | None = None,
) -> list[CreatedRecord]:
    query = _SELECT
    params: list[object] = [project_id]
    for column, value in (
        ("r.run_id", run_id),
        ("r.cleanup_status", cleanup_status.value if cleanup_status else None),
        ("runs.profile", profile),
    ):
        if value is not None:
            query += f" AND {column} = ?"
            params.append(value)
    query += " ORDER BY r.created_at, r.id"
    return [from_row(row) for row in conn.execute(query, params)]


def mark_cleaned(
    conn: sqlite3.Connection, project_id: str, tracking_id: str, timestamp: str
) -> CleanupResult:
    record = _select_one(conn, project_id, tracking_id)
    if record.cleanup_status == CleanupStatus.CLEANED_MANUALLY:
        return CleanupResult(record=record, changed=False)
    if record.cleanup_status != CleanupStatus.PENDING:
        raise CleanupTransitionError(
            f"o registro {tracking_id} não exige limpeza ({record.cleanup_status.value})"
        )
    updated = conn.execute(
        "UPDATE created_records SET cleanup_status = 'cleaned_manually', cleaned_at = ? "
        "WHERE id = ? AND cleanup_status = 'pending'",
        (timestamp, tracking_id),
    ).rowcount
    return CleanupResult(record=_select_one(conn, project_id, tracking_id), changed=updated == 1)
