import sqlite3
import sys
from pathlib import Path

import pytest

from lastro.history.database import Database
from lastro.history.errors import MigrationError
from lastro.history.migrations import MIGRATIONS, Migration, apply_migrations, schema_version

SOURCE_TABLES = {
    "source_catalogs",
    "document_sources",
    "source_generations",
    "source_documents",
    "source_generation_files",
    "source_chunks",
    "source_embeddings",
    "source_chunks_fts",
    "source_queries",
    "source_run_refs",
    "source_knowledge_refs",
    "source_conflicts",
    "source_conflict_entries",
    "source_conflict_requests",
    "retrieval_samples",
}
# Checksums publicados: uma migration já entregue nunca muda.
PUBLISHED = {
    1: "06727960c31b5ec32645b72ce5dd9b620cc9289e3f4e93f52c19d5e913ae19e9",
    2: "096bedd792360ac66ee29e47bd6b983da823e643e39f4c8bd0ab3dd0e8953f21",
    3: "2107f206de3056a965590b9f53ebae0a0c9aeaeeddc8d9222606c0691617ca2c",
    4: "c29892e163d8633a52f41e61b6bf2447f404ab580917de189621ab21335e2c7d",
    5: "9c1470a3b4efac2d8e73cc1a048aaae0e7ae22fc9bed742021b5e736346c4f6c",
    6: "deb428d1ec5b033ee9761cb969a5c76c7961fccfb65c663b4d20f513d48b891f",
    7: "bf5d56601919b20ea74ee588aed0c03d4c3b368bfa02721b953aafd25292b3c6",
    8: "46e6aba6c129d3c80d2d2d0ebd9141808c7cdd7c4e27ac6964efde39e08c0b46",
    9: "7078e9179727939d0e43c3dc8630333cf0ba4cd9c67d1b1f3822928f6725a04b",
    10: "a7ef56960462b768b9f0aad8ad865f6750287e764801071175c1a1cb74a32e9b",
    11: "0e558b01594f377436cc8bdbfe5558d5e9dfe7cff514b51bc66dd3e93391cd74",
    12: "24afd3b083521baf2481cf885d00e0eab216e1f3366cc6b0caf28c53d8a1bf06",
    13: "e950023d3f75ba1d71b6f622f0ed044edd0a4a2548d06a3e692dbabf399e8378",
}
HASH = "a" * 64
NOW = "2026-11-19T12:00:00.000000Z"


def _tables(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    finally:
        conn.close()


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def test_upgrade_v013_without_optional_extras(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("fastembed", "sqlite_vec", "pypdf", "bs4", "markdown_it", "ragas"):
        monkeypatch.setitem(sys.modules, name, None)
    path = tmp_path / "history.sqlite"
    database = Database(path)
    apply_migrations(database, MIGRATIONS[:13])
    conn = _connect(path)
    conn.execute(
        "INSERT INTO knowledge (id, project_id, payload_json, created_at, updated_at) "
        "VALUES ('old', 'p', '{}', ?, ?)",
        (NOW, NOW),
    )
    conn.close()

    assert apply_migrations(database) == [14, 15]

    assert schema_version(database) == 15
    assert _tables(path) >= SOURCE_TABLES
    conn = _connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM knowledge").fetchone() == (1,)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_migration_rollback_and_immutable_checksums(tmp_path: Path) -> None:
    for migration in MIGRATIONS[:13]:
        assert migration.checksum == PUBLISHED[migration.version], migration.version
    path = tmp_path / "history.sqlite"
    database = Database(path)
    apply_migrations(database, MIGRATIONS[:13])
    broken = Migration(14, MIGRATIONS[13].name, (*MIGRATIONS[13].statements, "CREATE TABLE x ("))

    with pytest.raises(MigrationError):
        apply_migrations(database, (*MIGRATIONS[:13], broken))

    assert schema_version(database) == 13
    assert not (_tables(path) & SOURCE_TABLES)
    assert apply_migrations(database) == [14, 15]


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO document_sources (id, project_id, canonical_root, root_key, display_label, "
        "created_at) VALUES ('src_1', 'p', '/docs', ?, 'docs', ?)",
        (HASH, NOW),
    )
    conn.execute(
        "INSERT INTO source_documents (id, source_id, relative_path, format, file_sha256, "
        "text_sha256, byte_size, extracted_characters, normalized_text, extraction_fingerprint, "
        "created_at) VALUES ('doc_1', 'src_1', 'a.md', 'markdown', ?, ?, 1, 1, 'x', ?, ?)",
        (HASH, HASH, HASH, NOW),
    )
    conn.execute(
        "INSERT INTO source_chunks (id, document_id, ordinal, heading, locator_json, text, "
        "token_count, content_sha256, chunking_fingerprint) "
        "VALUES ('chunk_1', 'doc_1', 0, 'h', '{}', 'x', 1, ?, ?)",
        (HASH, HASH),
    )


def test_invalid_fk_hash_and_vector_rejected(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite"
    apply_migrations(Database(path))
    conn = _connect(path)
    try:
        _seed(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO source_chunks (id, document_id, ordinal, heading, locator_json, "
                "text, token_count, content_sha256, chunking_fingerprint) "
                "VALUES ('c2', 'missing', 0, 'h', '{}', 'x', 1, ?, ?)",
                (HASH, HASH),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO source_chunks (id, document_id, ordinal, heading, locator_json, "
                "text, token_count, content_sha256, chunking_fingerprint) "
                "VALUES ('c3', 'doc_1', 1, 'h', '{}', 'x', 1, 'NOT-HEX', ?)",
                (HASH,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO source_chunks (id, document_id, ordinal, heading, locator_json, "
                "text, token_count, content_sha256, chunking_fingerprint) "
                "VALUES ('c4', 'doc_1', 2, 'h', '{}', 'x', 801, ?, ?)",
                (HASH, HASH),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO source_embeddings (chunk_id, model_fingerprint, window_ordinal, "
                "token_start, token_end, dimension, vector) VALUES ('chunk_1', ?, 0, 0, 1, "
                "1024, ?)",
                (HASH, b"\x00" * 100),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO source_documents (id, source_id, relative_path, format, "
                "file_sha256, text_sha256, byte_size, extracted_characters, normalized_text, "
                "extraction_fingerprint, created_at) VALUES ('d2', 'src_1', '../escape.md', "
                "'markdown', ?, ?, 1, 1, 'x', ?, ?)",
                (HASH, HASH, HASH, NOW),
            )
        conn.execute(
            "INSERT INTO source_embeddings (chunk_id, model_fingerprint, window_ordinal, "
            "token_start, token_end, dimension, vector) VALUES ('chunk_1', ?, 0, 0, 1, 1024, ?)",
            (HASH, b"\x00" * 4096),
        )
        conn.execute(
            "INSERT INTO source_generations (id, project_id, ordinal, status, fingerprint_json, "
            "fingerprint_sha256, lease_generation, created_at) "
            "VALUES ('g1', 'p', 1, 'staging', '{}', ?, 1, ?)",
            (HASH, NOW),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO source_generation_files (generation_id, source_id, relative_path, "
                "status) VALUES ('g1', 'src_1', 'a.md', 'ready')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO source_catalogs (project_id, active_generation_id, updated_at) "
                "VALUES ('other', 'g1', ?)",
                (NOW,),
            )
    finally:
        conn.close()
