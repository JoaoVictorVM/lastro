import sqlite3
from pathlib import Path
from typing import Any

import pytest

from lastro.config.loader import ProjectContext
from lastro.history import ArtifactType, RunStatus, TextMission, open_history
from lastro.history import store as store_module
from lastro.history.database import Database
from lastro.history.migrations import MIGRATIONS, apply_migrations, schema_version

REPRODUCTION_TABLES = (
    "reproduction_rounds",
    "reproduction_runs",
    "reproduction_reports",
    "finding_reproduction_verdicts",
)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@pytest.fixture
def v10_history(
    history_context: ProjectContext, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, str]:
    """Histórico real no schema v10 com run, artefato, eventos e uma tentativa legada."""
    monkeypatch.setattr(
        store_module, "apply_migrations", lambda db: apply_migrations(db, MIGRATIONS[:10])
    )
    history = open_history(history_context)
    run = history.create_run(history_context, TextMission("Explore employees"))
    history.append_event(run.record.id, "tool_call", tool_name="observe", correlation_id="a1")
    history.register_artifact(
        run.record.id, "evidence/a.png", ArtifactType.SCREENSHOT, content=b"a"
    )
    history.finish_run(run.record.id, RunStatus.COMPLETED)
    monkeypatch.undo()
    return history, run.record.id


def test_upgrade_v010_preserves_events_and_registers_checksum(
    v10_history: tuple[Any, str],
) -> None:
    history, run_id = v10_history
    database = Database(history.database_path)
    assert schema_version(database) == 10
    with _connect(history.database_path) as conn:
        events = [tuple(row) for row in conn.execute("SELECT * FROM events ORDER BY id")]

    assert apply_migrations(database) == [11]

    with _connect(history.database_path) as conn:
        assert [tuple(row) for row in conn.execute("SELECT * FROM events ORDER BY id")] == events
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        assert set(REPRODUCTION_TABLES) <= tables
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(finding_reproduction_attempts)")
        }
        assert {"ordinal", "reason", "note", "precondition_index", "verifier_executor"} <= columns
        checksum = conn.execute(
            "SELECT checksum_sha256 FROM schema_migrations WHERE version = 11"
        ).fetchone()[0]
        assert checksum == MIGRATIONS[10].checksum
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM events WHERE run_id = ?", (run_id,))
        conn.execute(
            "INSERT INTO events (run_id, sequence, timestamp, event_type, payload_json) "
            "VALUES (?, 999, '2026-11-07T00:00:00Z', 'reproduction_verdict', '{}')",
            (run_id,),
        )
    assert history.list_reproduction_verdicts(run_id) == []
