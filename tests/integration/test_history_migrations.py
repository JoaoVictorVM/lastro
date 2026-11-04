import sqlite3
from pathlib import Path

import pytest

from lastro.config.loader import ProjectContext
from lastro.history import MigrationError, TextMission, open_history
from lastro.history.database import Database
from lastro.history.migrations import MIGRATIONS, Migration, apply_migrations, schema_version
from lastro.history.runs import insert_run

TABLES = {
    "schema_migrations",
    "runs",
    "events",
    "artifacts",
    "criteria",
    "findings",
    "approvals",
    "created_records",
    "knowledge",
    "knowledge_history",
    "issue_snapshots",
    "eval_results",
}
INDEXES = {
    "idx_runs_started_at",
    "idx_runs_status_started_at",
    "idx_runs_profile_started_at",
    "idx_runs_issue_snapshot",
    "idx_events_run_sequence",
    "idx_events_run_type",
    "idx_events_correlation",
    "idx_events_timestamp",
    "idx_events_idempotency",
    "idx_criteria_run_outcome",
    "idx_criteria_run",
    "idx_findings_run_classification",
    "idx_findings_run",
    "idx_approvals_run_decision",
    "idx_approvals_fingerprint",
    "idx_approvals_pending",
    "idx_created_records_run_cleanup",
    "idx_created_records_cleanup_created",
    "idx_created_records_tenant_entity",
    "idx_knowledge_project_state",
    "idx_knowledge_run",
    "idx_knowledge_updated",
    "idx_knowledge_history_entry",
    "idx_knowledge_history_run",
    "idx_issue_snapshots_issue",
    "idx_issue_snapshots_content",
    "idx_eval_results_suite",
    "idx_eval_results_run",
    "idx_eval_results_executor",
}
FOREIGN_KEYS = {
    "runs": {("issue_snapshot_id", "issue_snapshots")},
    "events": {("run_id", "runs")},
    "artifacts": {("run_id", "runs")},
    "criteria": {("run_id", "runs")},
    "findings": {("run_id", "runs")},
    "approvals": {("run_id", "runs")},
    "created_records": {("run_id", "runs")},
    "knowledge": {("run_id", "runs")},
    "knowledge_history": {("knowledge_id", "knowledge"), ("run_id", "runs")},
    "eval_results": {("run_id", "runs")},
}


def _schema(path: Path) -> list[tuple[str, str, str | None]]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
            "ORDER BY type, name"
        ).fetchall()
    finally:
        conn.close()


def test_initial_migration_creates_complete_minimum_schema(tmp_path: Path) -> None:
    database = Database(tmp_path / "history.sqlite")

    assert apply_migrations(database, MIGRATIONS[:1]) == [1]

    with database.connection() as conn:
        objects = {
            (row["type"], row["name"]) for row in conn.execute("SELECT * FROM sqlite_master")
        }
        assert {name for kind, name in objects if kind == "table"} >= TABLES
        assert {name for kind, name in objects if kind == "index"} >= INDEXES
        for table, expected in FOREIGN_KEYS.items():
            keys = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            assert {(row["from"], row["table"]) for row in keys} == expected, table
            assert all(row["on_delete"] == "NO ACTION" for row in keys)
        run_columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(runs)")}
        assert run_columns["id"]["pk"] == 1
        assert run_columns["mission_text"]["notnull"] == 0
        assert run_columns["config_snapshot_json"]["notnull"] == 1
        payload = {row["name"]: row for row in conn.execute("PRAGMA table_info(criteria)")}
        assert payload["payload_json"]["dflt_value"] == "'{}'"
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        (migration,) = conn.execute("SELECT * FROM schema_migrations").fetchall()
        assert (migration["version"], migration["name"]) == (1, "initial")
        assert migration["checksum_sha256"] == MIGRATIONS[0].checksum
    assert schema_version(database) == 1
    assert apply_migrations(database, MIGRATIONS[:1]) == []


def test_failed_migration_leaves_database_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite"
    database = Database(path)
    apply_migrations(database)
    before = _schema(path)
    latest = MIGRATIONS[-1].version
    broken = Migration(
        version=latest + 1,
        name="broken",
        statements=(
            "CREATE TABLE extra (id TEXT PRIMARY KEY)",
            "ALTER TABLE runs ADD COLUMN extra_note TEXT",
            "INSERT INTO tabela_que_nao_existe VALUES (1)",
        ),
    )

    with pytest.raises(MigrationError) as info:
        apply_migrations(database, [*MIGRATIONS, broken])

    assert info.value.version == latest + 1
    assert str(info.value).startswith(
        f"A migração {latest + 1} do banco falhou; o banco não foi alterado. "
        "Execute 'lastro backup' e reporte o problema."
    )
    assert _schema(path) == before
    assert schema_version(database) == latest


def test_failed_first_migration_leaves_empty_database_without_version(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite"
    broken = Migration(version=1, name="initial", statements=("CREATE TABLE x (",))

    with pytest.raises(MigrationError):
        apply_migrations(Database(path), [broken])

    assert _schema(path) == []


def test_changed_published_migration_is_refused(tmp_path: Path) -> None:
    database = Database(tmp_path / "history.sqlite")
    apply_migrations(database)
    tampered = Migration(
        version=1, name="initial", statements=(*MIGRATIONS[0].statements, "SELECT 1")
    )

    with pytest.raises(MigrationError, match="checksum"):
        apply_migrations(database, [tampered])


def test_unknown_newer_version_is_refused(tmp_path: Path) -> None:
    database = Database(tmp_path / "history.sqlite")
    newer = Migration(
        version=MIGRATIONS[-1].version + 1,
        name="newer",
        statements=("CREATE TABLE newer (id TEXT)",),
    )
    apply_migrations(database, [*MIGRATIONS, newer])

    with pytest.raises(MigrationError, match="desconhecida"):
        apply_migrations(database)


def test_oracle_migration_preserves_existing_runs(
    tmp_path: Path,
    history_context: ProjectContext,
) -> None:
    database = Database(tmp_path / "history.sqlite")
    apply_migrations(database, MIGRATIONS[:1])
    store = open_history(history_context)
    record = store.create_run(history_context, TextMission("existing mission")).record
    database.write(lambda conn: insert_run(conn, record))
    before = database.read(
        lambda conn: conn.execute(
            "SELECT checksum_sha256 FROM schema_migrations WHERE version = 1"
        ).fetchone()[0]
    )
    assert apply_migrations(database) == [2, 3, 4, 5, 6, 7, 8]
    assert (
        database.read(
            lambda conn: conn.execute(
                "SELECT mission_text FROM runs WHERE id = ?", (record.id,)
            ).fetchone()[0]
        )
        == "existing mission"
    )
    assert (
        database.read(
            lambda conn: conn.execute(
                "SELECT checksum_sha256 FROM schema_migrations WHERE version = 1"
            ).fetchone()[0]
        )
        == before
    )
    assert (
        database.read(
            lambda conn: conn.execute("SELECT COUNT(*) FROM oracle_signals").fetchone()[0]
        )
        == 0
    )


def test_oracle_migration_failure_rolls_back(tmp_path: Path) -> None:
    database = Database(tmp_path / "history.sqlite")
    apply_migrations(database, MIGRATIONS[:1])
    before = _schema(database.path)
    oracle = MIGRATIONS[1]
    broken = Migration(oracle.version, oracle.name, (*oracle.statements, "SELECT * FROM missing"))
    with pytest.raises(MigrationError):
        apply_migrations(database, [MIGRATIONS[0], broken])
    assert _schema(database.path) == before
    assert schema_version(database) == 1


def _insert_created(
    database: Database, run_id: str, record_id: str, payload: str, created_at: str = "x"
) -> None:
    database.write(
        lambda conn: conn.execute(
            "INSERT INTO created_records (id, run_id, entity_type, system_identifier, tenant, "
            "payload_json, created_at) VALUES (?, ?, 'employee', '42', 'acme', ?, ?)",
            (record_id, run_id, payload, created_at),
        )
    )


def test_created_record_action_index_preserves_v001_rows(
    tmp_path: Path, history_context: ProjectContext
) -> None:
    database = Database(tmp_path / "history.sqlite")
    apply_migrations(database, MIGRATIONS[:2])
    record = open_history(history_context).create_run(history_context, TextMission("m")).record
    database.write(lambda conn: insert_run(conn, record))
    _insert_created(database, record.id, "legacy-1", "{}")
    _insert_created(database, record.id, "legacy-2", "{}")
    _insert_created(database, record.id, "legacy-3", '{"action_id": 7}')

    assert apply_migrations(database) == [3, 4, 5, 6, 7, 8]

    with database.connection() as conn:
        rows = conn.execute("SELECT id FROM created_records ORDER BY id").fetchall()
        indexes = {row["name"] for row in conn.execute("PRAGMA index_list(created_records)")}
    assert [row["id"] for row in rows] == ["legacy-1", "legacy-2", "legacy-3"]
    assert {
        "idx_created_records_run_action",
        "idx_created_records_run_created",
        "idx_created_records_run_cleanup",
        "idx_created_records_cleanup_created",
        "idx_created_records_tenant_entity",
    } <= indexes
    assert (
        database.read(
            lambda conn: conn.execute(
                "SELECT checksum_sha256 FROM schema_migrations WHERE version = 2"
            ).fetchone()[0]
        )
        == MIGRATIONS[1].checksum
    )


def test_duplicate_action_rejected(tmp_path: Path, history_context: ProjectContext) -> None:
    database = Database(tmp_path / "history.sqlite")
    apply_migrations(database)
    record = open_history(history_context).create_run(history_context, TextMission("m")).record
    database.write(lambda conn: insert_run(conn, record))
    _insert_created(database, record.id, "first", '{"action_id": "a1"}')

    with pytest.raises(sqlite3.IntegrityError):
        _insert_created(database, record.id, "second", '{"action_id": "a1"}')
    _insert_created(database, record.id, "other-action", '{"action_id": "a2"}')


def test_mission_migration_preserves_existing_runs_and_criteria(
    tmp_path: Path, history_context: ProjectContext
) -> None:
    database = Database(tmp_path / "history.sqlite")
    apply_migrations(database, MIGRATIONS[:3])
    record = open_history(history_context).create_run(history_context, TextMission("m")).record
    database.write(lambda conn: insert_run(conn, record))
    database.write(
        lambda conn: conn.execute(
            "INSERT INTO criteria (id, run_id, payload_json, created_at, updated_at) "
            "VALUES ('legacy', ?, '{\"flow\":\"cadastro\"}', 'x', 'x')",
            (record.id,),
        )
    )
    checksums = database.read(
        lambda conn: conn.execute(
            "SELECT version, checksum_sha256 FROM schema_migrations ORDER BY version"
        ).fetchall()
    )

    assert apply_migrations(database, MIGRATIONS[:4]) == [4]

    with database.connection() as conn:
        assert conn.execute("SELECT id FROM criteria").fetchall()[0]["id"] == "legacy"
        assert conn.execute("SELECT mission_text FROM runs").fetchone()[0] == "m"
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master")}
        current = conn.execute(
            "SELECT version, checksum_sha256 FROM schema_migrations WHERE version < 4 "
            "ORDER BY version"
        ).fetchall()
    assert {"mission_plans", "clarifications", "idx_criteria_run_normalized"} <= tables
    assert [tuple(row) for row in current] == [tuple(row) for row in checksums]


def test_mission_migration_rolls_back_on_failure(tmp_path: Path) -> None:
    database = Database(tmp_path / "history.sqlite")
    apply_migrations(database, MIGRATIONS[:3])
    before = _schema(database.path)
    mission = MIGRATIONS[3]
    broken = Migration(mission.version, mission.name, (*mission.statements, "SELECT * FROM x"))

    with pytest.raises(MigrationError):
        apply_migrations(database, [*MIGRATIONS[:3], broken])

    assert _schema(database.path) == before
    assert schema_version(database) == 3
