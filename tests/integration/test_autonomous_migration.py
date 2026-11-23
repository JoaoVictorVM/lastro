import sqlite3
from pathlib import Path

import pytest

from lastro.history.database import Database
from lastro.history.errors import MigrationError
from lastro.history.migrations import MIGRATIONS, Migration, apply_migrations, schema_version

AUTONOMOUS_TABLES = {
    "autonomous_schedules",
    "autonomous_occurrences",
    "autonomous_queue_items",
    "autonomous_profile_leases",
    "autonomous_comparisons",
    "autonomous_finding_matches",
    "autonomous_notifications",
    "autonomous_notification_attempts",
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
    14: "cb042c18917768193f665dca4f083708592f895ce44e68b10e0a28268ebcc3e8",
}
HASH = "a" * 64
NOW = "2026-11-23T10:00:00.000000Z"
RUN = "run_20261123_100000_abcd"


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _objects(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    rows = conn.execute("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
    return {(row[0], row[1]) for row in rows}


def _seed_v14(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO runs (id, project_id, profile, environment_url, tenant, mission_source, "
        "mission_text, started_at, ended_at, status, owner_pid, executor_name, lastro_version, "
        "config_snapshot_json) VALUES ('run_20261123_100000_abcd', 'p', 'homolog', 'https://h', "
        "'t', 'text', 'm', ?, ?, 'completed', 1, 'claude-code', '0', '{}')",
        (NOW, NOW),
    )
    conn.execute(
        "INSERT INTO events (run_id, sequence, timestamp, event_type, payload_json) "
        "VALUES ('run_20261123_100000_abcd', 1, ?, 'run_started', '{}')",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO report_snapshots (id, run_id, kind, input_sha256, status, error_text, "
        "created_at, updated_at) VALUES ('rs_1', ?, 'final', ?, 'failed', 'x', ?, ?)",
        (RUN, HASH, NOW, NOW),
    )


def test_upgrade_v014_preserves_rows_and_checksums(tmp_path: Path) -> None:
    for migration in MIGRATIONS[:14]:
        assert migration.checksum == PUBLISHED[migration.version], migration.version
    path = tmp_path / "history.sqlite"
    database = Database(path)
    apply_migrations(database, MIGRATIONS[:14])
    conn = _connect(path)
    _seed_v14(conn)
    conn.close()

    assert apply_migrations(database) == [15]
    assert schema_version(database) == 15

    conn = _connect(path)
    try:
        assert conn.execute("SELECT id, sequence, event_type FROM events").fetchall() == [
            (1, 1, "run_started")
        ]
        assert conn.execute("SELECT id, kind FROM report_snapshots").fetchall() == [
            ("rs_1", "final")
        ]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        names = {name for _, name in _objects(conn)}
        assert names >= AUTONOMOUS_TABLES
        upgraded = _objects(conn)
    finally:
        conn.close()
    fresh = tmp_path / "fresh.sqlite"
    apply_migrations(Database(fresh))
    other = _connect(fresh)
    try:
        assert _objects(other) == upgraded
    finally:
        other.close()


def test_report_kind_and_events_extended(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite"
    apply_migrations(Database(path))
    conn = _connect(path)
    try:
        _seed_v14(conn)
        conn.execute(
            "INSERT INTO report_snapshots (id, run_id, kind, input_sha256, status, error_text, "
            "created_at, updated_at) VALUES ('rs_2', ?, 'autonomous', ?, 'failed', 'x', ?, ?)",
            (RUN, HASH, NOW, NOW),
        )
        for sequence, kind in enumerate(
            (
                "autonomous_triggered",
                "autonomous_dequeued",
                "autonomous_compared",
                "autonomous_notification",
            ),
            start=2,
        ):
            conn.execute(
                "INSERT INTO events (run_id, sequence, timestamp, event_type, payload_json) "
                "VALUES ('run_20261123_100000_abcd', ?, ?, ?, '{}')",
                (sequence, NOW, kind),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO report_snapshots (id, run_id, kind, input_sha256, status, "
                "error_text, created_at, updated_at) VALUES ('rs_3', ?, 'weekly', ?, "
                "'failed', 'x', ?, ?)",
                (RUN, HASH, NOW, NOW),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM events")
    finally:
        conn.close()


def test_migration_rollback(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite"
    database = Database(path)
    apply_migrations(database, MIGRATIONS[:14])
    broken = Migration(15, MIGRATIONS[14].name, (*MIGRATIONS[14].statements, "CREATE TABLE x ("))
    with pytest.raises(MigrationError):
        apply_migrations(database, (*MIGRATIONS[:14], broken))
    assert schema_version(database) == 14
    conn = _connect(path)
    try:
        names = {name for _, name in _objects(conn)}
        assert not names & AUTONOMOUS_TABLES
        assert "events" in names and "report_snapshots" in names
    finally:
        conn.close()
    assert apply_migrations(database) == [15]


def _queue_row(conn: sqlite3.Connection, item_id: str, key: str, status: str) -> None:
    conn.execute(
        "INSERT INTO autonomous_queue_items (id, project_id, profile_name, trigger_kind, "
        "trigger_key, trigger_sha256, mission_snapshot_json, mission_sha256, "
        "trigger_snapshot_json, status, queued_at) VALUES (?, 'p', 'homolog', 'manual', ?, ?, "
        "'{}', ?, '{}', ?, ?)",
        (item_id, key, HASH, HASH, status, NOW),
    )


def test_foreign_keys_and_partial_uniques(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite"
    apply_migrations(Database(path))
    conn = _connect(path)
    try:
        _seed_v14(conn)
        _queue_row(conn, "aq_1", "k1", "running")
        with pytest.raises(sqlite3.IntegrityError):
            _queue_row(conn, "aq_2", "k2", "claimed")
        _queue_row(conn, "aq_3", "k3", "queued")
        with pytest.raises(sqlite3.IntegrityError):
            _queue_row(conn, "aq_4", "k3", "queued")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE autonomous_queue_items SET run_id = 'missing' WHERE id = 'aq_1'")
        conn.execute("UPDATE autonomous_queue_items SET run_id = ? WHERE id = 'aq_1'", (RUN,))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE autonomous_queue_items SET run_id = NULL WHERE id = 'aq_1'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE autonomous_queue_items SET status = 'dropped' WHERE id = 'aq_3'")
        conn.execute(
            "INSERT INTO autonomous_profile_leases (project_id, profile_name, generation, "
            "updated_at) VALUES ('p', 'homolog', 2, ?)",
            (NOW,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="decrease"):
            conn.execute("UPDATE autonomous_profile_leases SET generation = 1")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE autonomous_profile_leases SET token = 'only-token'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO autonomous_comparisons (id, queue_item_id, run_id, "
                "baseline_run_id, status, algorithm, algorithm_sha256, coverage_json, "
                "counts_json, limitations_json, created_at) VALUES ('acmp_1', 'aq_1', ?, ?, "
                "'compared', 'a', ?, '{}', '{}', '[]', ?)",
                (RUN, RUN, HASH, NOW),
            )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
