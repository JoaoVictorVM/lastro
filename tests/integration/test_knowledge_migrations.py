import json
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

import pytest

from lastro.config.loader import ProjectContext
from lastro.history import MigrationError, RunStatus, TextMission, open_history
from lastro.history.database import Database
from lastro.history.migrations import MIGRATIONS, Migration, apply_migrations, schema_version
from lastro.history.runs import insert_run
from lastro.knowledge import EditDocument, QueryInput

MEMORY_TABLES = {
    "knowledge_observations",
    "knowledge_pending",
    "knowledge_evidence",
    "knowledge_conflicts",
    "knowledge_run_refs",
    "knowledge_mutations",
    "run_memory_outcomes",
    "knowledge_consolidation_attempts",
    "knowledge_fts",
}


def tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_upgrade_v006_keeps_existing_data(tmp_path: Path, history_context: ProjectContext) -> None:
    database = Database(tmp_path / "history.sqlite")
    apply_migrations(database, MIGRATIONS[:6])
    record = open_history(history_context).create_run(history_context, TextMission("m")).record
    database.write(lambda conn: insert_run(conn, record))
    database.write(
        lambda conn: conn.execute(
            "INSERT INTO knowledge (id, project_id, run_id, validation_state, payload_json, "
            "created_at, updated_at) VALUES ('old', 'p', ?, 'observed', '{\"x\":1}', 'a', 'b')",
            (record.id,),
        )
    )
    database.write(
        lambda conn: conn.execute(
            "INSERT INTO knowledge_history (knowledge_id, run_id, payload_json, created_at) "
            "VALUES ('old', ?, '{}', 'a')",
            (record.id,),
        )
    )

    assert apply_migrations(database) == [7, 8]

    with database.connection() as conn:
        row = conn.execute("SELECT * FROM knowledge WHERE id = 'old'").fetchone()
        history = conn.execute("SELECT * FROM knowledge_history").fetchone()
        assert tables(conn) >= MEMORY_TABLES
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert (row["legacy"], row["payload_json"], row["flow"], row["revision"]) == (
        1,
        '{"x":1}',
        None,
        1,
    )
    assert (history["knowledge_id"], history["operation"]) == ("old", None)
    assert schema_version(database) == 8


def test_new_rows_require_typed_fields(tmp_path: Path) -> None:
    database = Database(tmp_path / "history.sqlite")
    apply_migrations(database)

    with pytest.raises(sqlite3.IntegrityError):
        database.write(
            lambda conn: conn.execute(
                "INSERT INTO knowledge (id, project_id, created_at, updated_at, legacy) "
                "VALUES ('k', 'p', 'a', 'b', 0)"
            )
        )


def test_legacy_knowledge_not_retrieved(knowledge: Any) -> None:
    history, run_id = knowledge.start()
    conn = sqlite3.connect(history.database_path)
    try:
        conn.execute(
            "INSERT INTO knowledge (id, project_id, validation_state, created_at, updated_at, "
            "flow, statement) VALUES ('legacy', ?, 'observed', 'a', 'b', 'employee', 'cpf')",
            (history.project_id,),
        )
        conn.execute(
            "INSERT INTO knowledge_fts (entry_id, statement, search_text) "
            "VALUES ('legacy', 'cpf', 'cpf')"
        )
        conn.commit()
    finally:
        conn.close()

    for text in ("", "cpf"):
        result = history.query_knowledge(run_id, QueryInput(flow="employee", query=text))
        assert result.returned_count == 0
    assert history.list_knowledge() == []


def test_missing_fts5_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite"
    database = Database(path)
    apply_migrations(database, MIGRATIONS[:6])
    statements = tuple(
        statement.replace("USING fts5(", "USING lastro_missing_fts(")
        for statement in MIGRATIONS[6].statements
    )
    broken = Migration(version=7, name=MIGRATIONS[6].name, statements=statements)

    with pytest.raises(MigrationError):
        apply_migrations(database, [*MIGRATIONS[:6], broken])

    assert schema_version(database) == 6
    with database.connection() as conn:
        assert not tables(conn) & MEMORY_TABLES
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(knowledge)")}
    assert "legacy" not in columns


def test_fts_tracks_edit_and_discard(knowledge: Any) -> None:
    history, run_id = knowledge.start()
    observation = knowledge.observe(history, run_id)
    knowledge.propose(history, run_id, observation)
    history.finish_run(run_id, RunStatus.COMPLETED)
    (entry,) = history.list_knowledge()

    edited = EditDocument.model_validate(
        {
            "fact": {
                "type": "field_property",
                "subject": "employee",
                "field": "cpf",
                "attribute": "visible",
                "value": True,
            }
        }
    )
    result = history.edit_knowledge(entry.id, edited, entry.revision, "Correção humana")
    history2, run2 = knowledge.start()

    assert result.status == "applied"
    assert (
        history2.query_knowledge(run2, QueryInput(flow="employee", query="visible")).returned_count
        == 1
    )
    assert (
        history2.query_knowledge(run2, QueryInput(flow="employee", query="required")).returned_count
        == 0
    )
    history2.finish_run(run2, RunStatus.COMPLETED)

    current = history.list_knowledge()[0]
    history.discard_knowledge(current.id, current.revision, "Obsoleto")
    history3, run3 = knowledge.start()
    assert (
        history3.query_knowledge(run3, QueryInput(flow="employee", query="visible")).returned_count
        == 0
    )


def test_history_and_fk_integrity(knowledge: Any) -> None:
    history, run_id = knowledge.start()
    observation = knowledge.observe(history, run_id)
    knowledge.propose(history, run_id, observation)
    knowledge.propose(history, run_id, observation, value=False)
    history.finish_run(run_id, RunStatus.COMPLETED)

    conn = sqlite3.connect(history.database_path)
    try:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        operations = [
            row[0] for row in conn.execute("SELECT operation FROM knowledge_history ORDER BY id")
        ]
        evidence = conn.execute("SELECT COUNT(*) FROM knowledge_evidence").fetchone()[0]
    finally:
        conn.close()
    assert operations.count("created") == 2
    assert "contested" in operations
    assert evidence == 2


def test_backup_contains_memory_tables(knowledge: Any, tmp_path: Path) -> None:
    history, run_id = knowledge.start()
    knowledge.propose(history, run_id, knowledge.observe(history, run_id))
    history.finish_run(run_id, RunStatus.COMPLETED)
    destination = tmp_path / "backups"
    destination.mkdir()

    result = history.backup(destination)

    restored = tmp_path / "restored"
    with zipfile.ZipFile(result.archive) as archive:
        archive.extractall(restored)
    manifest = json.loads((restored / "manifest.json").read_text(encoding="utf-8"))
    conn = sqlite3.connect(restored / history.project_id / "history.sqlite")
    try:
        assert tables(conn) >= MEMORY_TABLES
        assert conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM run_memory_outcomes").fetchone()[0] == 1
    finally:
        conn.close()
    assert manifest["schema_version"] == 8
