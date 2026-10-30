"""Ciclo de vida da run: identificador, registro, transições, ponteiro ativo e reconciliação."""

import contextlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from lastro.config.loader import ProjectContext
from lastro.history.errors import (
    InvalidMissionError,
    InvalidTransitionError,
    RunNotFoundError,
)
from lastro.history.events import PreparedEvent, insert_event, prepare_event
from lastro.history.layout import write_bytes_atomically
from lastro.history.models import (
    MAX_MISSION_TEXT_LENGTH,
    NON_TERMINAL_STATUSES,
    EventType,
    FileMission,
    IssueMission,
    JsonObject,
    JsonValue,
    Mission,
    MissionSource,
    RunRecord,
    RunStatus,
    TextMission,
    can_transition,
)
from lastro.history.redaction import canonical_json, redact, redact_text

RUN_ID_ENTROPY_BYTES = 2


def new_run_id(now: datetime, timezone: str, entropy_hex: str) -> str:
    # O horário do identificador é o do fuso de exibição, como o usuário lê no terminal.
    local = now.astimezone(ZoneInfo(timezone))
    return f"run_{local:%Y%m%d_%H%M%S}_{entropy_hex}"


@dataclass(frozen=True)
class ValidatedMission:
    source: MissionSource
    text: str | None = None
    file_bytes: bytes | None = None
    issue_snapshot_id: str | None = None


def validate_mission(mission: Mission) -> ValidatedMission:
    match mission:
        case TextMission(text=text):
            if not text.strip():
                raise InvalidMissionError("a missão em texto não pode ser vazia")
            if len(text) > MAX_MISSION_TEXT_LENGTH:
                raise InvalidMissionError(
                    f"a missão em texto aceita até {MAX_MISSION_TEXT_LENGTH} caracteres"
                )
            return ValidatedMission(source=MissionSource.TEXT, text=text)
        case FileMission(path=path):
            return ValidatedMission(source=MissionSource.FILE, file_bytes=_read_mission_file(path))
        case IssueMission(issue_snapshot_id=snapshot_id, text=text):
            if not snapshot_id.strip():
                raise InvalidMissionError("a missão de issue exige o id do snapshot")
            return ValidatedMission(
                source=MissionSource.ISSUE, text=text, issue_snapshot_id=snapshot_id
            )


def _read_mission_file(path: Path) -> bytes:
    if not path.is_file():
        raise InvalidMissionError(f"arquivo de missão não encontrado ou não regular: {path}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise InvalidMissionError(f"não foi possível ler a missão {path}: {exc}") from None
    if not data.strip():
        raise InvalidMissionError(f"o arquivo de missão está vazio: {path}")
    return data


def config_snapshot(context: ProjectContext) -> JsonObject:
    """Configuração efetiva da run, sem credenciais e com os limites preservados."""
    profile = context.profile.model_dump(mode="json", exclude={"credentials", "run_limits"})
    run_limits: JsonObject = {
        key: redact_text(value) if isinstance(value, str) else value
        for key, value in context.profile.run_limits.items()
        if isinstance(value, str | int | float)
    }
    return {
        "project_id": context.require_project_id(),
        "profile": context.profile_name,
        "timezone": context.timezone,
        "executor": context.config.executor,
        "environment": redact(profile),
        "run_limits": run_limits,
    }


def run_from_row(row: sqlite3.Row) -> RunRecord:
    snapshot: JsonObject = json.loads(row["config_snapshot_json"])
    return RunRecord(
        id=row["id"],
        project_id=row["project_id"],
        profile=row["profile"],
        environment_url=row["environment_url"],
        tenant=row["tenant"],
        mission_source=MissionSource(row["mission_source"]),
        mission_text=row["mission_text"],
        mission_snapshot_path=row["mission_snapshot_path"],
        mission_file_sha256=row["mission_file_sha256"],
        issue_snapshot_id=row["issue_snapshot_id"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        status=RunStatus(row["status"]),
        status_reason=row["status_reason"],
        owner_pid=row["owner_pid"],
        executor_name=row["executor_name"],
        executor_version=row["executor_version"],
        lastro_version=row["lastro_version"],
        skill_bundle_version=row["skill_bundle_version"],
        config_snapshot=snapshot,
        analyzed_code_version=row["analyzed_code_version"],
        executed_system_version=row["executed_system_version"],
    )


def insert_run(conn: sqlite3.Connection, record: RunRecord) -> None:
    conn.execute(
        "INSERT INTO runs (id, project_id, profile, environment_url, tenant, mission_source, "
        "mission_text, mission_snapshot_path, mission_file_sha256, issue_snapshot_id, "
        "started_at, ended_at, status, status_reason, owner_pid, executor_name, "
        "executor_version, lastro_version, skill_bundle_version, config_snapshot_json, "
        "analyzed_code_version, executed_system_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            record.id,
            record.project_id,
            record.profile,
            record.environment_url,
            record.tenant,
            record.mission_source.value,
            record.mission_text,
            record.mission_snapshot_path,
            record.mission_file_sha256,
            record.issue_snapshot_id,
            record.started_at,
            record.ended_at,
            record.status.value,
            record.status_reason,
            record.owner_pid,
            record.executor_name,
            record.executor_version,
            record.lastro_version,
            record.skill_bundle_version,
            canonical_json(record.config_snapshot),
            record.analyzed_code_version,
            record.executed_system_version,
        ),
    )


def select_run(conn: sqlite3.Connection, run_id: str) -> RunRecord:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise RunNotFoundError(run_id)
    return run_from_row(row)


def run_ended_event(status: RunStatus, reason: str | None) -> PreparedEvent:
    payload: dict[str, JsonValue] = {"status": status.value, "reason": reason}
    return prepare_event(EventType.RUN_ENDED, payload=payload)


def apply_transition(
    conn: sqlite3.Connection,
    run_id: str,
    target: RunStatus,
    reason: str | None,
    timestamp: str,
) -> RunRecord:
    """Valida e aplica a transição; um estado terminal grava ended_at e o evento run_ended."""
    current = select_run(conn, run_id)
    if not can_transition(current.status, target):
        raise InvalidTransitionError(
            f"transição inválida da run {run_id}: {current.status.value} -> {target.value}"
        )
    safe_reason = redact_text(reason) if reason is not None else None
    ended_at = timestamp if target.is_terminal else None
    conn.execute(
        "UPDATE runs SET status = ?, status_reason = ?, ended_at = ? WHERE id = ?",
        (target.value, safe_reason, ended_at, run_id),
    )
    if target.is_terminal:
        insert_event(conn, run_id, run_ended_event(target, safe_reason), timestamp)
    return select_run(conn, run_id)


def select_non_terminal(conn: sqlite3.Connection) -> list[RunRecord]:
    placeholders = ", ".join("?" for _ in NON_TERMINAL_STATUSES)
    rows = conn.execute(
        f"SELECT * FROM runs WHERE status IN ({placeholders}) ORDER BY started_at",
        tuple(status.value for status in NON_TERMINAL_STATUSES),
    ).fetchall()
    return [run_from_row(row) for row in rows]


@dataclass(frozen=True)
class ActivePointer:
    run_id: str
    owner_pid: int
    started_at: str


def publish_active_pointer(path: Path, record: RunRecord) -> None:
    data: JsonObject = {
        "run_id": record.id,
        "owner_pid": record.owner_pid,
        "started_at": record.started_at,
    }
    write_bytes_atomically(path, (canonical_json(data) + "\n").encode("utf-8"))


def read_active_pointer(path: Path) -> ActivePointer | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return ActivePointer(
            run_id=str(data["run_id"]),
            owner_pid=int(data["owner_pid"]),
            started_at=str(data["started_at"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def clear_active_pointer(path: Path, run_id: str) -> None:
    pointer = read_active_pointer(path)
    if pointer is not None and pointer.run_id == run_id:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
