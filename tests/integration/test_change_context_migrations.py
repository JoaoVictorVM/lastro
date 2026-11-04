import sqlite3
from pathlib import Path
from typing import Any

import pytest

from lastro.history import MigrationError, RunStatus
from lastro.history import store as store_module
from lastro.history.database import Database
from lastro.history.migrations import MIGRATIONS, Migration, apply_migrations, schema_version
from lastro.oracles.models import OracleOccurrence


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@pytest.fixture
def v8_history(knowledge: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, str]:
    """Histórico real no schema v8 com eventos, sinais, memória e um snapshot de issue."""
    monkeypatch.setattr(
        store_module, "apply_migrations", lambda db: apply_migrations(db, MIGRATIONS[:8])
    )
    history, run_id = knowledge.start()
    observation = knowledge.observe(history, run_id)
    knowledge.propose(history, run_id, observation)
    history.record_oracle_signal(
        run_id,
        OracleOccurrence(
            occurrence_id="occ-1",
            signal_type="js_error",
            url=None,
            url_path="/employees",
            http_method=None,
            http_status=None,
            message="TypeError",
            timestamp="2026-11-04T10:00:00.000000Z",
            action_id=None,
            action_sequence=None,
        ),
    )
    history.finish_run(run_id, RunStatus.COMPLETED)
    with _connect(history.database_path) as conn:
        conn.execute(
            "INSERT INTO issue_snapshots (id, project_id, repository, issue_number, "
            "content_sha256, payload_json, captured_at) "
            "VALUES ('issue_old', ?, 'org/repo', 7, ?, '{}', '2026-11-01T00:00:00Z')",
            (history.project_id, "e" * 64),
        )
        columns: list[str] = [row["name"] for row in conn.execute("PRAGMA table_info(runs)")]
        overrides = {
            "id": "'run_20261101_000000_beef'",
            "mission_source": "'issue'",
            "mission_text": "NULL",
            "issue_snapshot_id": "'issue_old'",
        }
        selected = ", ".join(overrides.get(name, name) for name in columns)
        conn.execute(
            f"INSERT INTO runs ({', '.join(columns)}) SELECT {selected} FROM runs WHERE id = ?",
            (run_id,),
        )
    monkeypatch.undo()
    return history, run_id


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = ("runs", "events", "oracle_signals", "knowledge_observations", "issue_snapshots")
    return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}


def test_upgrade_v008_preserves_runs_events_and_foreign_keys(
    v8_history: tuple[Any, str],
) -> None:
    history, run_id = v8_history
    database = Database(history.database_path)
    assert schema_version(database) == 8
    with _connect(history.database_path) as conn:
        before = _counts(conn)
        event_ids = [row[0] for row in conn.execute("SELECT id FROM events ORDER BY id")]

    assert apply_migrations(database) == [9]

    with _connect(history.database_path) as conn:
        assert _counts(conn) == before
        assert [row[0] for row in conn.execute("SELECT id FROM events ORDER BY id")] == event_ids
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        old = conn.execute("SELECT * FROM issue_snapshots WHERE id = 'issue_old'").fetchone()
        assert (old["source_kind"], old["source_identity"]) == ("github", "github:org/repo#7")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM events WHERE run_id = ?", (run_id,))
        conn.execute(
            "INSERT INTO events (run_id, sequence, timestamp, event_type, payload_json) "
            "VALUES (?, 999, '2026-11-04T00:00:00Z', 'security_event', '{}')",
            (run_id,),
        )


def test_migration_failure_rolls_back(v8_history: tuple[Any, str]) -> None:
    history, _ = v8_history
    database = Database(history.database_path)
    broken = Migration(
        version=9,
        name="change_context",
        statements=(*MIGRATIONS[8].statements, "SELECT * FROM missing_table"),
    )
    with _connect(history.database_path) as conn:
        before = _counts(conn)

    with pytest.raises(MigrationError):
        apply_migrations(database, (*MIGRATIONS[:8], broken))

    assert schema_version(database) == 8
    with _connect(history.database_path) as conn:
        assert _counts(conn) == before
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(issue_snapshots)")}
        assert "source_kind" not in columns
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        assert "change_contexts" not in tables


def test_orphan_reference_fails_and_reverts(v8_history: tuple[Any, str]) -> None:
    history, _ = v8_history
    database = Database(history.database_path)
    statements = list(MIGRATIONS[8].statements)
    reinsert = next(
        index for index, item in enumerate(statements) if "FROM issue_snapshots_v9_copy" in item
    )
    # Um pai que some na reconstrução deixa a run órfã: o COMMIT falha e a versão é revertida.
    statements.insert(reinsert + 1, "DELETE FROM issue_snapshots WHERE id = 'issue_old'")
    broken = Migration(9, "change_context", tuple(statements))

    with pytest.raises(MigrationError):
        apply_migrations(database, (*MIGRATIONS[:8], broken))

    assert schema_version(database) == 8
    with _connect(history.database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM issue_snapshots").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_unknown_version_and_checksum_still_fail(v8_history: tuple[Any, str]) -> None:
    history, _ = v8_history
    database = Database(history.database_path)
    apply_migrations(database)
    changed = Migration(9, "change_context", ("SELECT 1",))
    with pytest.raises(MigrationError, match="checksum"):
        apply_migrations(database, (*MIGRATIONS[:8], changed))
    with pytest.raises(MigrationError, match="desconhecida"):
        apply_migrations(database, MIGRATIONS[:8])


def test_offline_snapshots_allow_null_remote_identity(tmp_path: Path) -> None:
    database = Database(tmp_path / "history.sqlite")
    apply_migrations(database)
    with _connect(database.path) as conn:
        conn.execute(
            "INSERT INTO issue_snapshots (id, project_id, source_kind, source_identity, "
            "content_sha256, payload_json, captured_at) "
            "VALUES ('issue_md', 'p', 'markdown', 'markdown', ?, '{}', 'x')",
            ("a" * 64,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO issue_snapshots (id, project_id, source_kind, source_identity, "
                "content_sha256, payload_json, captured_at) "
                "VALUES ('issue_gh', 'p', 'github', 'github:x', ?, '{}', 'x')",
                ("b" * 64,),
            )
