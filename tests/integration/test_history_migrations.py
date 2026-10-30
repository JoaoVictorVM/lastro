import sqlite3
from pathlib import Path

import pytest

from lastro.history import MigrationError
from lastro.history.database import Database
from lastro.history.migrations import MIGRATIONS, Migration, apply_migrations, schema_version

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

    assert apply_migrations(database) == [1]

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
    assert apply_migrations(database) == []


def test_failed_migration_leaves_database_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite"
    database = Database(path)
    apply_migrations(database)
    before = _schema(path)
    broken = Migration(
        version=2,
        name="broken",
        statements=(
            "CREATE TABLE extra (id TEXT PRIMARY KEY)",
            "ALTER TABLE runs ADD COLUMN extra_note TEXT",
            "INSERT INTO tabela_que_nao_existe VALUES (1)",
        ),
    )

    with pytest.raises(MigrationError) as info:
        apply_migrations(database, [*MIGRATIONS, broken])

    assert info.value.version == 2
    assert str(info.value).startswith(
        "A migração 2 do banco falhou; o banco não foi alterado. "
        "Execute 'lastro backup' e reporte o problema."
    )
    assert _schema(path) == before
    assert schema_version(database) == 1


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
    newer = Migration(version=2, name="newer", statements=("CREATE TABLE newer (id TEXT)",))
    apply_migrations(database, [*MIGRATIONS, newer])

    with pytest.raises(MigrationError, match="desconhecida"):
        apply_migrations(database)
