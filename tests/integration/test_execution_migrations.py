import sqlite3
from pathlib import Path
from typing import Any

import pytest

from lastro.config.loader import ProjectContext
from lastro.history import MigrationError, RunStatus, TextMission, open_history
from lastro.history import store as store_module
from lastro.history.database import Database
from lastro.history.migrations import MIGRATIONS, Migration, apply_migrations, schema_version

EXECUTION_TABLES = (
    "execution_controls",
    "execution_actions",
    "write_intents",
    "run_links",
    "write_reconciliations",
)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@pytest.fixture
def v9_history(history_context: ProjectContext, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, str]:
    """Histórico real no schema v9 com uma run terminada e eventos."""
    monkeypatch.setattr(
        store_module, "apply_migrations", lambda db: apply_migrations(db, MIGRATIONS[:9])
    )
    history = open_history(history_context)
    run = history.create_run(history_context, TextMission("Explore employees"))
    history.append_event(run.record.id, "tool_call", tool_name="observe", correlation_id="a1")
    history.finish_run(run.record.id, RunStatus.INTERRUPTED, "process_lost")
    monkeypatch.undo()
    return history, run.record.id


def test_upgrade_v009_preserves_events_and_foreign_keys(v9_history: tuple[Any, str]) -> None:
    history, run_id = v9_history
    database = Database(history.database_path)
    assert schema_version(database) == 9
    with _connect(history.database_path) as conn:
        events = [tuple(row) for row in conn.execute("SELECT * FROM events ORDER BY id")]

    assert apply_migrations(database) == [10, 11, 12]

    with _connect(history.database_path) as conn:
        assert [tuple(row) for row in conn.execute("SELECT * FROM events ORDER BY id")] == events
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        assert set(EXECUTION_TABLES) <= tables
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM events WHERE run_id = ?", (run_id,))
        conn.execute(
            "INSERT INTO events (run_id, sequence, timestamp, event_type, payload_json) "
            "VALUES (?, 999, '2026-11-05T00:00:00Z', 'run_resumed', '{}')",
            (run_id,),
        )
    # Run legada: limites só normalizados em memória, sem contadores inventados.
    state = history.execution_state(run_id)
    assert (state.max_actions, state.actions_reserved, state.stop_status) == (40, 0, None)


def test_failed_migration_rolls_back(v9_history: tuple[Any, str]) -> None:
    history, _ = v9_history
    database = Database(history.database_path)
    broken = Migration(
        version=10,
        name="execution_control",
        statements=(*MIGRATIONS[9].statements, "SELECT * FROM missing_table"),
    )
    with pytest.raises(MigrationError):
        apply_migrations(database, (*MIGRATIONS[:9], broken))
    assert schema_version(database) == 9
    with _connect(history.database_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        assert not set(EXECUTION_TABLES) & tables
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] > 0


def test_backup_preserves_execution_state(history_context: ProjectContext, tmp_path: Path) -> None:
    import zipfile

    history = open_history(history_context)
    run = history.create_run(history_context, TextMission("Explore employees"))
    history.reserve_action(run.record.id, "a1", "observe")
    history.latch_stop(run.record.id, RunStatus.CANCELLED, "user_cancelled")
    history.finish_run(run.record.id, RunStatus.CANCELLED, "user_cancelled")
    destination = tmp_path / "backups"
    destination.mkdir()
    history.backup(destination)
    (archive,) = destination.iterdir()
    restored = tmp_path / "restored"
    with zipfile.ZipFile(archive) as opened:
        opened.extractall(restored)
    database = restored / history_context.require_project_id() / "history.sqlite"
    with _connect(database) as conn:
        row = conn.execute(
            "SELECT stop_status, stop_reason, actions_reserved FROM execution_controls"
        ).fetchone()
        assert tuple(row) == ("cancelled", "user_cancelled", 1)
        assert conn.execute("SELECT COUNT(*) FROM execution_actions").fetchone()[0] == 1
