import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from lastro.history import (
    NON_TERMINAL_STATUSES,
    TERMINAL_STATUSES,
    ArtifactType,
    EventType,
    MissionSource,
    PolicyDecision,
    RunFilter,
    RunStatus,
    can_transition,
)
from lastro.history.database import Database
from lastro.history.migrations import apply_migrations

HASH = "a" * 64
RUN_ID = "run_20261030_143205_a1b2"
NOW = "2026-10-30T17:32:05.000000Z"


def test_status_enum_matches_operational_states() -> None:
    assert {status.value for status in RunStatus} == {
        "running",
        "awaiting_approval",
        "awaiting_clarification",
        "completed",
        "blocked",
        "interrupted",
        "cancelled",
        "limit_reached",
    }
    assert {status.value for status in NON_TERMINAL_STATUSES} == {
        "running",
        "awaiting_approval",
        "awaiting_clarification",
    }
    assert TERMINAL_STATUSES.isdisjoint(NON_TERMINAL_STATUSES)


def test_event_and_value_enums_are_stable() -> None:
    assert [event.value for event in EventType] == [
        "run_started",
        "tool_call",
        "policy_decision",
        "authentication",
        "approval_requested",
        "approval_decided",
        "clarification_requested",
        "clarification_answered",
        "oracle_signal",
        "finding_submitted",
        "record_created",
        "knowledge_proposed",
        "limit_hit",
        "run_ended",
    ]
    assert {decision.value for decision in PolicyDecision} == {
        "allow",
        "deny",
        "require_approval",
    }
    assert {source.value for source in MissionSource} == {"text", "file", "issue"}
    assert {kind.value for kind in ArtifactType} == {"screenshot", "video", "report", "snapshot"}


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATUSES))
def test_terminal_states_are_immutable(terminal: RunStatus) -> None:
    for target in RunStatus:
        assert not can_transition(terminal, target)


@pytest.mark.parametrize("waiting", [RunStatus.AWAITING_APPROVAL, RunStatus.AWAITING_CLARIFICATION])
def test_waiting_states_return_to_running_or_end(waiting: RunStatus) -> None:
    assert can_transition(waiting, RunStatus.RUNNING)
    for terminal in TERMINAL_STATUSES:
        assert can_transition(waiting, terminal)
    (other,) = NON_TERMINAL_STATUSES - {waiting, RunStatus.RUNNING}
    assert not can_transition(waiting, other)
    assert not can_transition(waiting, waiting)


def test_running_can_wait_or_end() -> None:
    for target in RunStatus:
        assert can_transition(RunStatus.RUNNING, target) is (target != RunStatus.RUNNING)


def test_run_filter_defaults_to_twenty_results() -> None:
    assert RunFilter().limit == 20


@pytest.fixture
def connection(tmp_path: Path) -> Callable[[], sqlite3.Connection]:
    database = Database(tmp_path / "history.sqlite")
    apply_migrations(database)

    def connect() -> sqlite3.Connection:
        conn = sqlite3.connect(tmp_path / "history.sqlite", isolation_level=None)
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    conn = connect()
    conn.execute(
        "INSERT INTO issue_snapshots (id, project_id, repository, issue_number, content_sha256, "
        "captured_at) VALUES ('iss_1', 'p', 'org/repo', 42, ?, ?)",
        (HASH, NOW),
    )
    conn.execute(
        "INSERT INTO runs (id, project_id, profile, environment_url, tenant, mission_source, "
        "mission_text, started_at, status, owner_pid, executor_name, lastro_version, "
        "config_snapshot_json) VALUES (?, 'p', 'homolog', 'https://x.test', 'acme', 'text', "
        "'missão', ?, 'running', 1, 'claude-code', '0.1.0', '{}')",
        (RUN_ID, NOW),
    )
    conn.execute(
        "INSERT INTO knowledge (id, project_id, created_at, updated_at) VALUES ('k1', 'p', ?, ?)",
        (NOW, NOW),
    )
    conn.close()
    return connect


VALID_ROWS = {
    "criteria": (
        "INSERT INTO criteria (id, run_id, outcome, created_at, updated_at) "
        "VALUES ('c1', ?, 'met', ?, ?)",
        (RUN_ID, NOW, NOW),
    ),
    "findings": (
        "INSERT INTO findings (id, run_id, classification, created_at, updated_at) "
        "VALUES ('f1', ?, 'suspected', ?, ?)",
        (RUN_ID, NOW, NOW),
    ),
    "approvals": (
        "INSERT INTO approvals (id, run_id, action_fingerprint, requested_at) "
        "VALUES ('a1', ?, ?, ?)",
        (RUN_ID, HASH, NOW),
    ),
    "created_records": (
        "INSERT INTO created_records (id, run_id, entity_type, system_identifier, tenant, "
        "created_at) VALUES ('r1', ?, 'employee', '1234', 'acme', ?)",
        (RUN_ID, NOW),
    ),
    "knowledge_history": (
        "INSERT INTO knowledge_history (knowledge_id, run_id, created_at) VALUES ('k1', ?, ?)",
        (RUN_ID, NOW),
    ),
    "eval_results": (
        "INSERT INTO eval_results (id, run_id, suite_id, condition, executor_name, created_at) "
        "VALUES ('e1', ?, 'suite', 'baseline', 'claude-code', ?)",
        (RUN_ID, NOW),
    ),
}

INVALID_ROWS = [
    (
        "INSERT INTO criteria (id, run_id, outcome, created_at, updated_at) "
        "VALUES ('c2', ?, 'talvez', ?, ?)",
        (RUN_ID, NOW, NOW),
    ),
    (
        "INSERT INTO criteria (id, run_id, payload_json, created_at, updated_at) "
        "VALUES ('c3', ?, 'não é json', ?, ?)",
        (RUN_ID, NOW, NOW),
    ),
    (
        "INSERT INTO criteria (id, run_id, created_at, updated_at) "
        "VALUES ('c4', 'run_inexistente', ?, ?)",
        (NOW, NOW),
    ),
    (
        "INSERT INTO findings (id, run_id, classification, created_at, updated_at) "
        "VALUES ('f2', ?, 'certeza', ?, ?)",
        (RUN_ID, NOW, NOW),
    ),
    (
        "INSERT INTO approvals (id, run_id, action_fingerprint, requested_at) "
        "VALUES ('a2', ?, 'curto', ?)",
        (RUN_ID, NOW),
    ),
    (
        "INSERT INTO approvals (id, run_id, action_fingerprint, decision, requested_at) "
        "VALUES ('a3', ?, ?, 'approve', ?)",
        (RUN_ID, HASH, NOW),
    ),
    (
        "INSERT INTO created_records (id, run_id, entity_type, system_identifier, tenant, "
        "created_at, cleaned_at) VALUES ('r2', ?, 'employee', '1', 'acme', ?, ?)",
        (RUN_ID, NOW, NOW),
    ),
    (
        "INSERT INTO created_records (id, run_id, entity_type, system_identifier, tenant, "
        "created_at) VALUES ('r3', ?, ' ', '1', 'acme', ?)",
        (RUN_ID, NOW),
    ),
    (
        "INSERT INTO knowledge (id, project_id, validation_state, created_at, updated_at) "
        "VALUES ('k2', 'p', 'achismo', ?, ?)",
        (NOW, NOW),
    ),
    (
        "INSERT INTO knowledge_history (knowledge_id, created_at) VALUES ('k_inexistente', ?)",
        (NOW,),
    ),
    (
        "INSERT INTO issue_snapshots (id, project_id, repository, issue_number, content_sha256, "
        "captured_at) VALUES ('iss_2', 'p', 'org/repo', 0, ?, ?)",
        (HASH, NOW),
    ),
    (
        "INSERT INTO issue_snapshots (id, project_id, repository, issue_number, content_sha256, "
        "captured_at) VALUES ('iss_3', 'p', 'org/repo', 42, ?, ?)",
        (HASH, NOW),
    ),
    (
        "INSERT INTO eval_results (id, suite_id, condition, executor_name, created_at) "
        "VALUES ('e2', 'suite', '', 'claude-code', ?)",
        (NOW,),
    ),
]


def test_reserved_tables_accept_minimum_payload_and_reject_invalid_contracts(
    connection: Callable[[], sqlite3.Connection],
) -> None:
    conn = connection()
    for table, (statement, params) in VALID_ROWS.items():
        conn.execute(statement, params)
        payload = conn.execute(f"SELECT payload_json FROM {table}").fetchone()[0]
        assert payload == "{}", table
    conn.execute(
        "UPDATE approvals SET decision = 'approve', decided_at = ? WHERE id = 'a1'", (NOW,)
    )
    conn.execute(
        "UPDATE created_records SET cleanup_status = 'cleaned_manually', cleaned_at = ? "
        "WHERE id = 'r1'",
        (NOW,),
    )

    for statement, params in INVALID_ROWS:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(statement, params)
    conn.close()
