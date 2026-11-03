import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from lastro.config.loader import ProjectContext
from lastro.history import (
    ApprovalChoice,
    ApprovalError,
    ApprovalNotPendingError,
    ApprovalStatus,
    DecidedBy,
    EventType,
    HistoryStore,
    JsonObject,
    NotificationChannel,
    NotificationStatus,
    RunStatus,
    TextMission,
    open_history,
)
from lastro.history.database import Database
from lastro.history.migrations import MIGRATIONS, apply_migrations, schema_version

FINGERPRINT = "a" * 64
OTHER = "b" * 64


def started(context: ProjectContext, clock: Callable[[], datetime]) -> tuple[HistoryStore, str]:
    history = open_history(context, clock=clock)
    run_id = history.create_run(context, TextMission("Editar funcionário")).record.id
    return history, run_id


def payload() -> JsonObject:
    return {"summary": "edit employee #57 (phone) in Clínica Aurora", "components": {}}


def event_types(history: HistoryStore, run_id: str) -> list[EventType]:
    return [event.event_type for event in history.list_events(run_id)]


def test_request_is_durable_and_moves_run_to_awaiting(
    history_context: ProjectContext, fake_clock: Callable[[], datetime]
) -> None:
    history, run_id = started(history_context, fake_clock)

    record = history.request_approval(run_id, "act1", FINGERPRINT, payload())

    assert record.status == ApprovalStatus.PENDING
    assert record.expires_at > record.requested_at
    assert history.get_run(run_id).status == RunStatus.AWAITING_APPROVAL
    requested = history.list_events(run_id)[-1]
    assert requested.event_type == EventType.APPROVAL_REQUESTED
    assert isinstance(requested.payload, dict)
    assert requested.payload["command"] == "lastro approve act1"
    with pytest.raises(ApprovalError) as error:
        history.request_approval(run_id, "act2", OTHER, payload())
    assert error.value.code == "run_not_running"


def test_cli_decision_is_recorded_once(
    history_context: ProjectContext, fake_clock: Callable[[], datetime]
) -> None:
    history, run_id = started(history_context, fake_clock)
    history.request_approval(run_id, "act1", FINGERPRINT, payload())

    decided = history.decide_approval("act1", ApprovalChoice.APPROVE, DecidedBy.CLI, note="ok")

    assert decided.status == ApprovalStatus.APPROVED
    assert decided.decided_by == DecidedBy.CLI
    assert history.get_run(run_id).status == RunStatus.RUNNING
    decision = history.approval_decision("act1")
    assert decision is not None
    assert (decision.decision, decision.decided_by) == (ApprovalChoice.APPROVE, DecidedBy.CLI)
    for choice in ApprovalChoice:
        with pytest.raises(ApprovalNotPendingError) as error:
            history.decide_approval("act1", choice, DecidedBy.CLI)
        assert str(error.value) == "No pending approval with id act1."
    with pytest.raises(ApprovalNotPendingError):
        history.decide_approval("unknown", ApprovalChoice.SKIP, DecidedBy.CLI)
    assert event_types(history, run_id).count(EventType.APPROVAL_DECIDED) == 1


def test_consumption_is_single_use_and_bound_to_fingerprint(
    history_context: ProjectContext, fake_clock: Callable[[], datetime]
) -> None:
    history, run_id = started(history_context, fake_clock)
    history.request_approval(run_id, "act1", FINGERPRINT, payload())
    history.decide_approval("act1", "approve", "cli")

    with pytest.raises(ApprovalError) as changed:
        history.consume_approval(run_id, "act1", OTHER, "retry1")
    assert changed.value.code == "fingerprint_changed"
    consumed = history.consume_approval(run_id, "act1", FINGERPRINT, "retry1")
    assert consumed.status == ApprovalStatus.CONSUMED
    assert consumed.execution_correlation_id == "retry1"
    with pytest.raises(ApprovalError) as again:
        history.consume_approval(run_id, "act1", FINGERPRINT, "retry2")
    assert again.value.code == "approval_not_approved"

    failed = history.fail_approval_execution(run_id, "act1", "browser_crash")
    assert failed.status == ApprovalStatus.EXECUTION_FAILED
    with pytest.raises(ApprovalError):
        history.consume_approval(run_id, "act1", FINGERPRINT, "retry3")


def test_invalidation_keeps_run_running_and_allows_new_request(
    history_context: ProjectContext, fake_clock: Callable[[], datetime]
) -> None:
    history, run_id = started(history_context, fake_clock)
    history.request_approval(run_id, "act1", FINGERPRINT, payload())
    history.decide_approval("act1", "approve", "cli")

    invalidated = history.invalidate_approval(run_id, "act1", "values")
    renewed = history.request_approval(run_id, "act2", OTHER, payload())

    assert invalidated.status == ApprovalStatus.INVALIDATED
    assert invalidated.invalidated_at is not None
    assert renewed.status == ApprovalStatus.PENDING
    last = [e for e in history.list_events(run_id) if e.event_type == EventType.APPROVAL_DECIDED]
    assert isinstance(last[-1].payload, dict)
    assert last[-1].payload["changed_field"] == "values"


def test_restart_reconciles_expired_pending_request(
    history_context: ProjectContext, clock_now: list[datetime], fake_clock: Callable[[], datetime]
) -> None:
    history, run_id = started(history_context, fake_clock)
    history.request_approval(run_id, "act1", FINGERPRINT, payload())
    clock_now[0] += timedelta(seconds=899)
    assert history.expire_approvals() == []

    clock_now[0] += timedelta(seconds=1)
    reopened = open_history(history_context, clock=fake_clock)

    record = reopened.get_approval("act1")
    assert record is not None
    assert record.status == ApprovalStatus.EXPIRED
    assert (record.decision, record.decided_by) == (ApprovalChoice.SKIP, DecidedBy.SYSTEM)
    assert record.decision_reason == "no_response"
    assert reopened.get_run(run_id).status == RunStatus.RUNNING
    with pytest.raises(ApprovalNotPendingError):
        reopened.decide_approval("act1", "approve", "cli")


def test_end_decision_cancels_run_atomically(
    history_context: ProjectContext, fake_clock: Callable[[], datetime]
) -> None:
    history, run_id = started(history_context, fake_clock)
    history.request_approval(run_id, "act1", FINGERPRINT, payload())

    ended = history.decide_approval("act1", "end_run", "cli")

    assert ended.status == ApprovalStatus.ENDED
    run = history.get_run(run_id)
    assert run.status == RunStatus.CANCELLED
    assert run.status_reason == "ended_at_approval_gate"
    assert history.get_active_run() is None
    assert event_types(history, run_id)[-2:] == [EventType.APPROVAL_DECIDED, EventType.RUN_ENDED]


def test_two_terminals_cannot_decide_same_request_twice(
    history_context: ProjectContext, fake_clock: Callable[[], datetime]
) -> None:
    history, run_id = started(history_context, fake_clock)
    history.request_approval(run_id, "act1", FINGERPRINT, payload())
    barrier = threading.Barrier(4)
    outcomes: list[str] = []

    def terminal(choice: str) -> None:
        store = open_history(history_context, clock=fake_clock)
        barrier.wait()
        try:
            store.decide_approval("act1", choice, "cli")
            outcomes.append("decided")
        except ApprovalNotPendingError:
            outcomes.append("rejected")

    threads = [
        threading.Thread(target=terminal, args=(choice,))
        for choice in ("approve", "skip", "approve", "end_run")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["decided", "rejected", "rejected", "rejected"]
    assert event_types(history, run_id).count(EventType.APPROVAL_DECIDED) == 1


def test_approval_never_writes_clarification_or_knowledge(
    history_context: ProjectContext, fake_clock: Callable[[], datetime]
) -> None:
    history, run_id = started(history_context, fake_clock)
    history.request_approval(run_id, "act1", FINGERPRINT, payload())
    history.decide_approval("act1", "approve", "cli")
    history.consume_approval(run_id, "act1", FINGERPRINT, "retry")

    conn = sqlite3.connect(history.database_path)
    try:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("approvals", "clarifications", "knowledge", "knowledge_history")
        }
    finally:
        conn.close()
    assert counts == {"approvals": 1, "clarifications": 0, "knowledge": 0, "knowledge_history": 0}


def test_notification_degradation_is_audited_once(
    history_context: ProjectContext, fake_clock: Callable[[], datetime]
) -> None:
    history, run_id = started(history_context, fake_clock)
    record = history.request_approval(run_id, "act1", FINGERPRINT, payload())

    for _ in range(2):
        history.record_notification(
            record,
            NotificationChannel.DESKTOP,
            NotificationStatus.DEGRADED,
            requested_at=fake_clock(),
            latency_ms=12,
            error_code="notifier_missing",
        )

    (attempt,) = history.list_notifications(record)
    assert attempt.status == NotificationStatus.DEGRADED
    degraded = [
        e for e in history.list_events(run_id) if e.event_type == EventType.NOTIFICATION_DEGRADED
    ]
    assert len(degraded) == 1
    assert history.get_approval("act1").status == ApprovalStatus.PENDING  # type: ignore[union-attr]


def test_v006_upgrades_v005_database_and_preserves_events(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite"
    database = Database(path)
    apply_migrations(database, MIGRATIONS[:5])
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    now = "2026-11-03T12:00:00.000000Z"
    run_id = "run_20261103_120000_abcd"
    conn.execute(
        "INSERT INTO runs (id, project_id, profile, environment_url, tenant, mission_source, "
        "mission_text, started_at, status, owner_pid, executor_name, lastro_version, "
        "config_snapshot_json) VALUES (?, 'p', 'homolog', 'https://x', 'acme', 'text', 'm', "
        "?, 'running', 1, 'claude-code', '0', '{}')",
        (run_id, now),
    )
    conn.execute(
        "INSERT INTO events (run_id, sequence, timestamp, event_type) "
        "VALUES (?, 1, ?, 'run_started')",
        (run_id, now),
    )
    conn.execute(
        "INSERT INTO oracle_signals (id, run_id, signal_type, url_path, message, dedupe_sha256, "
        "first_seen_at, last_seen_at, first_occurrence_json, last_occurrence_json, "
        "last_event_id) VALUES ('sig', ?, 'js_error', '/', 'boom', ?, ?, ?, '{}', '{}', 1)",
        (run_id, "c" * 64, now, now),
    )
    conn.execute(
        "INSERT INTO approvals (id, run_id, action_fingerprint, requested_at) "
        "VALUES ('legacy', ?, ?, ?)",
        (run_id, FINGERPRINT, now),
    )
    conn.close()

    assert apply_migrations(database) == [6]
    assert schema_version(database) == 6

    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT last_event_id FROM oracle_signals").fetchone() == (1,)
        legacy = conn.execute(
            "SELECT action_id, status, decision, decided_by FROM approvals"
        ).fetchone()
        assert legacy == ("legacy", "expired", "skip", "system")
        conn.execute(
            "INSERT INTO events (run_id, sequence, timestamp, event_type) "
            "VALUES (?, 2, ?, 'notification_degraded')",
            (run_id, now),
        )
        assert conn.execute("SELECT MAX(id) FROM events").fetchone() == (2,)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM events")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO oracle_signals (id, run_id, signal_type, url_path, message, "
                "dedupe_sha256, first_seen_at, last_seen_at, first_occurrence_json, "
                "last_occurrence_json, last_event_id) "
                "VALUES ('sig2', ?, 'js_error', '/', 'x', ?, ?, ?, '{}', '{}', 999)",
                (run_id, "d" * 64, now, now),
            )
    finally:
        conn.close()
