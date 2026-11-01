import json
import sqlite3
import zipfile

import pytest

from lastro.config.loader import ProjectContext
from lastro.history import (
    EventType,
    HistoryUnavailableError,
    InvalidEventError,
    RunNotActiveError,
    TextMission,
    open_history,
)
from lastro.history.database import Database
from lastro.oracles.models import OracleOccurrence


def occurrence(identifier: str = "occurrence_one", **changes: object) -> OracleOccurrence:
    return OracleOccurrence.model_validate(
        {
            "occurrence_id": identifier,
            "signal_type": "http_5xx",
            "url": "https://example.test/api/employee?private=yes#fragment",
            "url_path": "/ignored",
            "http_method": "PUT",
            "http_status": 500,
            "message": "HTTP 500",
            "timestamp": "2026-11-01T15:00:04.000000Z",
            "action_id": None,
            "action_sequence": None,
            **changes,
        }
    )


def test_identical_signal_stored_once_with_count(history_context: ProjectContext) -> None:
    store = open_history(history_context)
    run_id = store.create_run(history_context, TextMission("observe")).record.id
    first = store.record_oracle_signal(run_id, occurrence())
    action = store.append_event(run_id, EventType.TOOL_CALL, tool_name="submit", correlation_id="a")
    second = store.record_oracle_signal(
        run_id,
        occurrence(
            "occurrence_two",
            action_id="a",
            action_sequence=action.sequence,
            url="https://other.test/api/employee?different=true",
            http_method="POST",
        ),
    )
    assert first.id == second.id
    assert second.occurrence_count == 2
    assert second.first_occurrence.action_id is None
    assert second.last_occurrence.action_id == "a"
    assert second.last_occurrence.url == "https://other.test/api/employee"
    assert store.record_oracle_signal(run_id, occurrence()).occurrence_count == 2
    signals = store.list_oracle_signals(run_id)
    assert signals == [second]
    assert (
        len([e for e in store.list_events(run_id) if e.event_type == EventType.ORACLE_SIGNAL]) == 2
    )
    store.finish_run(run_id, "completed")
    assert open_history(history_context).list_oracle_signals(run_id) == signals
    with pytest.raises(RunNotActiveError):
        store.record_oracle_signal(run_id, occurrence("late"))
    other = store.create_run(history_context, TextMission("second")).record.id
    assert store.record_oracle_signal(other, occurrence()).id != first.id
    with pytest.raises(InvalidEventError):
        store.record_oracle_signal(
            other, occurrence("cross", action_id="a", action_sequence=action.sequence)
        )


def test_signal_and_event_commit_atomically(history_context: ProjectContext) -> None:
    store = open_history(history_context)
    run_id = store.create_run(history_context, TextMission("observe")).record.id
    database = Database(store.database_path)
    database.write(
        lambda conn: conn.execute(
            "CREATE TRIGGER fail_oracle BEFORE INSERT ON oracle_signals "
            "BEGIN SELECT RAISE(ABORT, 'test rollback'); END"
        )
    )
    before = store.list_events(run_id)
    with pytest.raises(sqlite3.IntegrityError):
        store.record_oracle_signal(run_id, occurrence())
    assert store.list_events(run_id) == before
    assert store.list_oracle_signals(run_id) == []


def test_backup_restores_oracle_signals_and_events(history_context: ProjectContext) -> None:
    store = open_history(history_context)
    run_id = store.create_run(history_context, TextMission("observe")).record.id
    signal = store.record_oracle_signal(run_id, occurrence())
    store.record_oracle_signal(run_id, occurrence("two"))
    backup = store.backup(history_context.project_data_dir.parent / "oracle-backup")
    restored = history_context.project_data_dir.parent / "restored"
    with zipfile.ZipFile(backup.archive) as archive:
        archive.extractall(restored)
    database = Database(restored / store.project_id / "history.sqlite")
    with database.connection() as conn:
        row = conn.execute("SELECT * FROM oracle_signals").fetchone()
        assert row["id"] == signal.id
        assert row["occurrence_count"] == 2
        events = conn.execute(
            "SELECT id FROM events WHERE event_type = 'oracle_signal' ORDER BY sequence"
        ).fetchall()
        assert len(events) == 2
        assert row["last_event_id"] == events[-1]["id"]


def test_write_failure_uses_redacted_fallback_and_lock(
    history_context: ProjectContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = open_history(history_context)
    run_id = store.create_run(history_context, TextMission("failure")).record.id

    def fail(*args: object, **kwargs: object) -> None:
        raise HistoryUnavailableError()

    with monkeypatch.context() as patch:
        patch.setattr(Database, "write", fail)
        with pytest.raises(HistoryUnavailableError):
            store.record_oracle_signal(run_id, occurrence(message="password=fictional-test-value"))
    fallback = store.run_paths(run_id).run_dir / "fallback-events.jsonl"
    text = fallback.read_text(encoding="utf-8")
    assert "fictional-test-value" not in text
    assert json.loads(text)["payload"]["message"] == "password=[REDACTED]"
    with pytest.raises(HistoryUnavailableError):
        store.record_oracle_signal(run_id, occurrence("later"))
    with pytest.raises(HistoryUnavailableError):
        store.list_oracle_signals(run_id)
