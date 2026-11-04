import sqlite3
from typing import Any

import pytest

from lastro.history import RunStatus
from lastro.history import store as store_module
from lastro.history.database import Database
from lastro.history.migrations import MIGRATIONS, apply_migrations, schema_version
from lastro.oracles.models import OracleOccurrence

NATIVE_TABLES = {"native_budgets", "native_run_contexts", "native_model_calls"}


def schema(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
        "AND name NOT LIKE 'knowledge_fts%'"
    ).fetchall()
    return {f"{row[0]}:{row[1]}": " ".join((row[2] or "").split()) for row in rows}


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("events", "oracle_signals", "knowledge_observations", "knowledge_evidence")
    }


@pytest.fixture
def v7_history(knowledge: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, str]:
    """Histórico real gravado com o schema v7, antes desta versão existir."""
    monkeypatch.setattr(
        store_module, "apply_migrations", lambda db: apply_migrations(db, MIGRATIONS[:7])
    )
    monkeypatch.setattr(store_module.HistoryStore, "reconcile_model_calls", lambda self: [])
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
    monkeypatch.undo()
    return history, run_id


def test_upgrade_preserves_event_ids_and_foreign_keys(v7_history: tuple[Any, str]) -> None:
    history, run_id = v7_history
    database = Database(history.database_path)
    assert schema_version(database) == 7
    with database.connection() as conn:
        before_events = [tuple(row) for row in conn.execute("SELECT * FROM events ORDER BY id")]
        before_counts = counts(conn)
    assert before_counts["knowledge_evidence"] >= 1
    assert before_counts["oracle_signals"] == 1

    assert apply_migrations(database) == [8]

    with database.connection() as conn:
        after_events = [tuple(row) for row in conn.execute("SELECT * FROM events ORDER BY id")]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert counts(conn) == before_counts
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert after_events == before_events
    assert names >= NATIVE_TABLES
    assert {"idx_events_run_sequence", "events_are_append_only_update"} <= names
    assert [e.sequence for e in history.list_events(run_id)] == list(
        range(1, len(before_events) + 1)
    )


def test_fresh_and_upgrade_schema_match(v7_history: tuple[Any, str], tmp_path: Any) -> None:
    history, _ = v7_history
    upgraded = Database(history.database_path)
    apply_migrations(upgraded)
    fresh = Database(tmp_path / "fresh.sqlite")
    apply_migrations(fresh)
    with upgraded.connection() as left, fresh.connection() as right:
        assert schema(left) == schema(right)


def test_new_events_obey_append_only(native_harness: Any) -> None:
    history = native_harness.history()
    run_id = native_harness.start(history)
    history.append_event(run_id, "agent_failure", payload={"code": "unknown_tool"})
    with sqlite3.connect(history.database_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE events SET payload_json = '{}' WHERE event_type = 'native_started'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM events WHERE event_type = 'agent_failure'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE native_budgets SET cap_nusd = 1")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO events (run_id, sequence, timestamp, event_type) "
                "VALUES (?, 99, 't', 'model_call_bogus')",
                (run_id,),
            )
    types = [event.event_type.value for event in history.list_events(run_id)]
    assert types[:3] == ["run_started", "native_started", "agent_failure"]


def test_reconcile_dead_owner_keeps_uncertain_cost(native_harness: Any) -> None:
    history = native_harness.history(owner_pid=999_999, process_checker=lambda pid: pid != 999_999)
    run_id = native_harness.start(history, cap_nusd=100_000)
    unsent = history.reserve_model_call(native_harness.reservation(run_id, 1000))
    sent = history.reserve_model_call(native_harness.reservation(run_id, 2000, step=2))
    history.mark_model_call_sent(sent.id)

    later = native_harness.history()
    record = later.get_run(run_id)
    assert record.status == RunStatus.INTERRUPTED
    calls = {call.id: call for call in later.list_model_calls(run_id)}
    assert calls[unsent.id].status == "released"
    assert calls[sent.id].status == "uncertain"
    usage = later.native_usage(run_id)
    assert usage.committed_nusd == 2000
    assert usage.confirmed_nusd == 0
    assert later.get_run(run_id).status == RunStatus.INTERRUPTED
