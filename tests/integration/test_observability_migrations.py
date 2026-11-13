import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from lastro.config.loader import ProjectContext
from lastro.history import TextMission, open_history
from lastro.history import observability as obs_module
from lastro.history import store as store_module
from lastro.history.database import Database
from lastro.history.migrations import MIGRATIONS, apply_migrations, schema_version


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def test_upgrade_preserves_events_and_checksums(
    obs_project: Callable[..., ProjectContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = obs_project(enabled=False)
    monkeypatch.setattr(
        store_module, "apply_migrations", lambda db: apply_migrations(db, MIGRATIONS[:11])
    )
    history = open_history(project)
    run = history.create_run(project, TextMission("Explore employees"))
    history.append_event(run.record.id, "tool_call", tool_name="observe", correlation_id="a1")
    monkeypatch.undo()
    database = Database(history.database_path)
    with _connect(history.database_path) as conn:
        events = [tuple(row) for row in conn.execute("SELECT * FROM events ORDER BY id")]

    assert apply_migrations(database) == [12]

    assert schema_version(database) == 12
    with _connect(history.database_path) as conn:
        assert [tuple(row) for row in conn.execute("SELECT * FROM events ORDER BY id")] == events
        row = conn.execute(
            "SELECT name, checksum_sha256 FROM schema_migrations WHERE version = 12"
        ).fetchone()
        assert (row["name"], row["checksum_sha256"]) == ("observability", MIGRATIONS[11].checksum)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_context_is_atomic_with_run_creation(
    obs_project: Callable[..., ProjectContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = obs_project()
    history = open_history(project)

    def broken(*_: object, **__: object) -> None:
        raise sqlite3.OperationalError("no such table: observability_runs")

    monkeypatch.setattr(obs_module, "insert_context", broken)
    with pytest.raises(sqlite3.OperationalError):
        history.create_run(project, TextMission("Explore employees"))

    with _connect(history.database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert history.get_active_run() is None


def test_immutable_context_and_descriptors(obs_project: Callable[..., ProjectContext]) -> None:
    project = obs_project()
    history = open_history(project)
    run = history.create_run(project, TextMission("Explore employees"))
    history.append_event(run.record.id, "oracle_signal", payload={"kind": "console"})
    history.project_observability(run.record.id)

    with _connect(history.database_path) as conn:
        for statement in (
            "UPDATE observability_runs SET endpoint = 'https://evil.example.test'",
            "UPDATE observability_runs SET suite_id = 'x', condition_name = 'y'",
            "UPDATE observability_spans SET descriptor_json = '{}' "
            "WHERE descriptor_json IS NOT NULL",
            "UPDATE observability_spans SET span_id = 'aaaaaaaaaaaaaaaa'",
            "DELETE FROM observability_spans",
            "DELETE FROM observability_runs",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(statement)


def test_cross_run_event_is_rejected(obs_project: Callable[..., ProjectContext]) -> None:
    project = obs_project()
    history = open_history(project)
    first = history.create_run(project, TextMission("first"))
    foreign = history.append_event(first.record.id, "oracle_signal", payload={"kind": "x"})
    history.finish_run(first.record.id, "completed")
    second = history.create_run(project, TextMission("second"))

    with _connect(history.database_path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO observability_spans (event_id, run_id, event_sequence, span_id, "
            "descriptor_json, descriptor_sha256, state) VALUES (?, ?, ?, ?, '{}', ?, 'pending')",
            (foreign.id, second.record.id, foreign.sequence, "abcdefabcdefabcd", "0" * 64),
        )


def test_old_runs_remain_disabled(
    obs_project: Callable[..., ProjectContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        store_module, "apply_migrations", lambda db: apply_migrations(db, MIGRATIONS[:11])
    )
    legacy_project = obs_project(enabled=False)
    history = open_history(legacy_project)
    legacy = history.create_run(legacy_project, TextMission("legacy"))
    history.finish_run(legacy.record.id, "completed")
    monkeypatch.undo()

    upgraded = open_history(obs_project())

    assert upgraded.observability_context(legacy.record.id) is None
    assert upgraded.observability_status(legacy.record.id) is None
    assert upgraded.report_inputs(legacy.record.id).trace is None
