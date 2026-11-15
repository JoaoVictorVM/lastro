import sqlite3
from pathlib import Path

import pytest

from lastro.history import MigrationError
from lastro.history.database import Database
from lastro.history.migrations import MIGRATIONS, Migration, apply_migrations, schema_version

NOW = "2026-11-14T10:00:00.000000Z"
HASH = "a" * 64


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def test_upgrade_v012_preserves_existing_history(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite"
    database = Database(path)
    apply_migrations(database, MIGRATIONS[:12])
    conn = connect(path)
    conn.execute(
        "INSERT INTO eval_results (id, suite_id, condition, executor_name, created_at) "
        "VALUES ('legacy', 'old-suite', 'full', 'codex', ?)",
        (NOW,),
    )
    before = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    conn.close()
    assert apply_migrations(database) == [13]
    conn = connect(path)
    row = conn.execute("SELECT * FROM eval_results WHERE id = 'legacy'").fetchone()
    assert row is not None
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == before
    fresh = tmp_path / "fresh.sqlite"
    apply_migrations(Database(fresh))
    upgraded = {(item[0], item[1]) for item in conn.execute("SELECT type, name FROM sqlite_master")}
    conn.close()
    other = connect(fresh)
    created = {(item[0], item[1]) for item in other.execute("SELECT type, name FROM sqlite_master")}
    other.close()
    assert upgraded == created
    assert schema_version(database) == 13


def test_migration_failure_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite"
    database = Database(path)
    apply_migrations(database, MIGRATIONS[:12])
    broken = Migration(13, "evaluation", (*MIGRATIONS[12].statements, "CREATE TABLE broken ("))
    with pytest.raises(MigrationError):
        apply_migrations(database, (*MIGRATIONS[:12], broken))
    assert schema_version(database) == 12
    conn = connect(path)
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    columns = {row[1] for row in conn.execute("PRAGMA table_info(eval_results)")}
    conn.close()
    assert "eval_suites" not in names and "revision" not in columns


def _suite(conn: sqlite3.Connection, suite_id: str = "eval_1") -> None:
    conn.execute(
        "INSERT INTO eval_suites (id, suite_name, schema_version, suite_sha256, manifest_json, "
        "catalog_sha256, rubric_sha256, executor_name, executor_version, model_identity_json, "
        "seed, created_at, updated_at) "
        "VALUES (?, 's', 1, ?, '{}', ?, ?, 'codex', '1', '{}', 0, ?, ?)",
        (suite_id, HASH, HASH, HASH, NOW, NOW),
    )


def _trial(conn: sqlite3.Connection, trial_id: str, ordinal: int, **extra: object) -> None:
    columns = {
        "id": trial_id,
        "suite_id": "eval_1",
        "ordinal": ordinal,
        "condition": "full",
        "scenario": "base",
        "variant": "base",
        "repetition": ordinal,
        "role": "measured",
        "condition_sha256": HASH,
        "created_at": NOW,
        **extra,
    }
    names = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    conn.execute(f"INSERT INTO eval_trials ({names}) VALUES ({marks})", tuple(columns.values()))


def test_foreign_keys_and_trial_selection_are_enforced(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite"
    apply_migrations(Database(path))
    conn = connect(path)
    _suite(conn)
    _trial(conn, "t1", 1)
    _trial(conn, "t2", 2)
    for trial in ("t1", "t2"):
        conn.execute(
            "INSERT INTO eval_attempts (id, trial_id, attempt, status, history_relative_path, "
            "executor_identity_json) VALUES (?, ?, 1, 'valid', 'evals/x', '{}')",
            (f"a-{trial}", trial),
        )
    conn.execute("UPDATE eval_trials SET selected_attempt_id = 'a-t1' WHERE id = 't1'")
    with pytest.raises(sqlite3.IntegrityError, match="another trial"):
        conn.execute("UPDATE eval_trials SET selected_attempt_id = 'a-t2' WHERE id = 't1'")
    with pytest.raises(sqlite3.IntegrityError):
        _trial(conn, "t3", 3, suite_id="missing")
    with pytest.raises(sqlite3.IntegrityError, match="warmup"):
        _trial(conn, "t4", 4, parent_trial_id="t1")
    with pytest.raises(sqlite3.IntegrityError):
        _trial(conn, "t5", 1)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO eval_attempts (id, trial_id, attempt, status, history_relative_path, "
            "executor_identity_json, access_start, access_end) "
            "VALUES ('bad', 't2', 2, 'valid', 'evals/x', '{}', 10, 5)"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO eval_attempts (id, trial_id, attempt, status, history_relative_path, "
            "executor_identity_json) VALUES ('esc', 't2', 3, 'valid', '../escape', '{}')"
        )
    with pytest.raises(sqlite3.IntegrityError, match="incomplete"):
        conn.execute(
            "INSERT INTO eval_results (id, suite_id, condition, executor_name, created_at, "
            "revision) VALUES ('r', 'eval_1', 'full', 'codex', ?, 1)",
            (NOW,),
        )
    conn.close()


def test_reviews_and_events_are_append_only(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite"
    apply_migrations(Database(path))
    conn = connect(path)
    _suite(conn)
    conn.execute(
        "INSERT INTO eval_events (suite_id, sequence, event_type, payload_json, idempotency_key, "
        "timestamp) VALUES ('eval_1', 1, 'x', '{}', 'k', ?)",
        (NOW,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE eval_events SET event_type = 'y'")
    conn.close()
