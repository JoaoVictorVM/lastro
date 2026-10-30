"""Event log append-only: sequência por run, idempotência, consulta incremental e fallback."""

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from lastro.history.errors import InvalidEventError
from lastro.history.models import (
    CORRELATED_EVENTS,
    EventRecord,
    EventType,
    JsonValue,
    PolicyDecision,
)
from lastro.history.redaction import RedactedJson, canonical_json, redact_text, redact_to_json


@dataclass(frozen=True)
class PreparedEvent:
    """Evento já redigido; nada aqui contém o valor original de um segredo."""

    event_type: EventType
    tool_name: str | None
    arguments: RedactedJson | None
    policy_decision: PolicyDecision | None
    correlation_id: str | None
    idempotency_key: str | None
    payload: RedactedJson | None
    result: RedactedJson | None

    def fallback_entry(self, run_id: str, timestamp: str, error: str) -> dict[str, JsonValue]:
        return {
            "run_id": run_id,
            "timestamp": timestamp,
            "event_type": self.event_type.value,
            "tool_name": self.tool_name,
            "arguments_sha256": self.arguments.sha256 if self.arguments else None,
            "arguments_preview": self.arguments.preview if self.arguments else None,
            "policy_decision": self.policy_decision.value if self.policy_decision else None,
            "correlation_id": self.correlation_id,
            "idempotency_key": self.idempotency_key,
            "payload": self.payload.value if self.payload else None,
            "result": self.result.value if self.result else None,
            "error": error,
        }


def _optional_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if not value.strip():
        raise InvalidEventError(f"{field} não pode ser vazio")
    return value


def _payload_subtype(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    subtype = cast(dict[object, object], payload).get("subtype")
    return subtype if isinstance(subtype, str) else None


def prepare_event(
    event_type: EventType | str,
    *,
    payload: object = None,
    result: object = None,
    tool_name: str | None = None,
    arguments: object = None,
    policy_decision: PolicyDecision | str | None = None,
    correlation_id: str | None = None,
    idempotency_key: str | None = None,
) -> PreparedEvent:
    """Valida o contrato do tipo e redige tudo antes de qualquer serialização."""
    try:
        kind = EventType(event_type)
    except ValueError:
        raise InvalidEventError(f"tipo de evento desconhecido: {event_type}") from None
    decision: PolicyDecision | None = None
    if policy_decision is not None:
        try:
            decision = PolicyDecision(policy_decision)
        except ValueError:
            raise InvalidEventError(f"decisão de política inválida: {policy_decision}") from None
    if kind == EventType.POLICY_DECISION and decision is None:
        raise InvalidEventError("evento policy_decision exige a decisão")
    if kind != EventType.POLICY_DECISION and decision is not None:
        raise InvalidEventError("decisão de política só é aceita em evento policy_decision")
    correlated = correlation_id is not None or idempotency_key is not None
    if kind not in CORRELATED_EVENTS and correlated:
        raise InvalidEventError("correlation id e idempotency key só valem para tool e policy")
    if kind == EventType.AUTHENTICATION:
        subtype = _payload_subtype(payload)
        if subtype is None or not subtype.strip():
            raise InvalidEventError("evento authentication exige payload.subtype não vazio")
    return PreparedEvent(
        event_type=kind,
        tool_name=redact_text(tool_name) if tool_name is not None else None,
        arguments=redact_to_json(arguments) if arguments is not None else None,
        policy_decision=decision,
        correlation_id=_optional_text(correlation_id, "correlation_id"),
        idempotency_key=_optional_text(idempotency_key, "idempotency_key"),
        payload=redact_to_json(payload) if payload is not None else None,
        result=redact_to_json(result) if result is not None else None,
    )


def _load_json(text: str | None) -> JsonValue:
    if text is None:
        return None
    value: JsonValue = json.loads(text)
    return value


def event_from_row(row: sqlite3.Row) -> EventRecord:
    decision = row["policy_decision"]
    return EventRecord(
        id=row["id"],
        run_id=row["run_id"],
        sequence=row["sequence"],
        timestamp=row["timestamp"],
        event_type=EventType(row["event_type"]),
        tool_name=row["tool_name"],
        arguments_sha256=row["arguments_sha256"],
        arguments_preview=row["arguments_preview"],
        policy_decision=PolicyDecision(decision) if decision is not None else None,
        correlation_id=row["correlation_id"],
        idempotency_key=row["idempotency_key"],
        payload=_load_json(row["payload_json"]),
        result=_load_json(row["result_json"]),
    )


def find_idempotent(
    conn: sqlite3.Connection, run_id: str, event: PreparedEvent
) -> EventRecord | None:
    if event.idempotency_key is None:
        return None
    row = conn.execute(
        "SELECT * FROM events WHERE run_id = ? AND event_type = ? AND idempotency_key = ?",
        (run_id, event.event_type.value, event.idempotency_key),
    ).fetchone()
    return event_from_row(row) if row is not None else None


def insert_event(
    conn: sqlite3.Connection, run_id: str, event: PreparedEvent, timestamp: str
) -> EventRecord:
    """Insere com a próxima sequência; deve rodar dentro de uma transação de escrita."""
    existing = find_idempotent(conn, run_id, event)
    if existing is not None:
        return existing
    row = conn.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 AS next FROM events WHERE run_id = ?", (run_id,)
    ).fetchone()
    sequence = int(row["next"])
    cursor = conn.execute(
        "INSERT INTO events (run_id, sequence, timestamp, event_type, tool_name, "
        "arguments_sha256, arguments_preview, policy_decision, correlation_id, "
        "idempotency_key, payload_json, result_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            sequence,
            timestamp,
            event.event_type.value,
            event.tool_name,
            event.arguments.sha256 if event.arguments else None,
            event.arguments.preview if event.arguments else None,
            event.policy_decision.value if event.policy_decision else None,
            event.correlation_id,
            event.idempotency_key,
            event.payload.canonical if event.payload else None,
            event.result.canonical if event.result else None,
        ),
    )
    inserted = conn.execute("SELECT * FROM events WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return event_from_row(inserted)


def select_events(
    conn: sqlite3.Connection, run_id: str, after_sequence: int = 0, limit: int | None = None
) -> list[EventRecord]:
    query = "SELECT * FROM events WHERE run_id = ? AND sequence > ? ORDER BY sequence"
    params: tuple[object, ...] = (run_id, after_sequence)
    if limit is not None:
        query += " LIMIT ?"
        params = (*params, limit)
    return [event_from_row(row) for row in conn.execute(query, params).fetchall()]


def write_fallback(path: Path, entry: dict[str, JsonValue]) -> None:
    """Acrescenta uma linha JSON já redigida e sincroniza antes de retornar."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(entry) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
