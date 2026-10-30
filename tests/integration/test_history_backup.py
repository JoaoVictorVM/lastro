import json
import sqlite3
import zipfile
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lastro.cli import history_cmd
from lastro.cli.app import app
from lastro.config.loader import ProjectContext
from lastro.history import (
    ArtifactType,
    FileMission,
    HistoryStore,
    RunStatus,
    TextMission,
    open_history,
    verify_restored_backup,
)


def _populate(
    context: ProjectContext,
    clock_now: list[datetime],
    fake_clock: Callable[[], datetime],
    tmp_path: Path,
) -> HistoryStore:
    store = open_history(context, clock=fake_clock)
    mission = tmp_path / "missao.md"
    mission.write_text("# Missão\nCadastrar funcionário\n", encoding="utf-8")
    statuses = [RunStatus.COMPLETED, RunStatus.BLOCKED, RunStatus.LIMIT_REACHED]
    for index, status in enumerate(statuses):
        clock_now[0] -= timedelta(hours=5)
        source = FileMission(mission) if index == 0 else TextMission(f"Missão {index}")
        created = store.create_run(context, source)
        store.append_event(created.record.id, "oracle_signal", payload={"status": 500 + index})
        if index == 1:
            store.register_artifact(
                created.record.id,
                "evidence/step-01.png",
                ArtifactType.SCREENSHOT,
                content=b"png" * 100,
            )
        clock_now[0] += timedelta(minutes=12)
        store.finish_run(created.record.id, status, "motivo" if status != "completed" else None)
    conn = sqlite3.connect(store.database_path)
    try:
        run_id = store.list_runs()[0].record.id
        conn.execute(
            "INSERT INTO findings (id, run_id, classification, created_at, updated_at) "
            "VALUES ('f1', ?, 'confirmed', 'x', 'x')",
            (run_id,),
        )
        conn.execute(
            "INSERT INTO criteria (id, run_id, payload_json, created_at, updated_at) "
            "VALUES ('c1', ?, '{\"flow\":\"cadastro\"}', 'x', 'x')",
            (run_id,),
        )
        conn.commit()
    finally:
        conn.close()
    return store


def test_backup_restored_manually_to_empty_folder_preserves_history_output(
    runner: CliRunner,
    history_context: ProjectContext,
    clock_now: list[datetime],
    fake_clock: Callable[[], datetime],
    tmp_path: Path,
    workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _populate(history_context, clock_now, fake_clock, tmp_path)
    monkeypatch.setattr(history_cmd, "clock", fake_clock)
    # Uma conexão aberta mantém dados no WAL, que o backup online precisa capturar.
    keeper = sqlite3.connect(store.database_path)
    keeper.execute("SELECT 1 FROM runs").fetchone()
    try:
        assert store.database_path.with_name("history.sqlite-wal").exists()
        original = runner.invoke(app, ["history", "list", "--limit", "50"])
        destination = tmp_path / "backups"
        destination.mkdir()
        backup = runner.invoke(app, ["backup", str(destination)])
    finally:
        keeper.close()
    assert original.exit_code == 0, original.output
    assert backup.exit_code == 0, backup.output
    (archive,) = destination.iterdir()

    restored_root = tmp_path / "restaurado"
    restored_root.mkdir()
    with zipfile.ZipFile(archive) as opened:
        names = opened.namelist()
        opened.extractall(restored_root)
    project_id = history_context.require_project_id()
    assert "manifest.json" in names
    assert f"{project_id}/history.sqlite" in names
    assert not any(name.endswith(("-wal", "-shm")) for name in names)
    assert verify_restored_backup(restored_root) == []
    manifest = json.loads((restored_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["project_id"] == project_id
    assert manifest["schema_version"] == 1
    restored_runs = restored_root / project_id / "runs"
    for summary in store.list_runs():
        assert (restored_runs / summary.record.id / "evidence").is_dir()

    verify = workdir / "verificacao"
    verify.mkdir()
    config = history_context.config_path.read_text(encoding="utf-8")
    config = config.replace(history_context.private_data_root.as_posix(), restored_root.as_posix())
    (verify / "lastro.toml").write_text(config, encoding="utf-8")
    monkeypatch.chdir(verify)
    restored = runner.invoke(app, ["history", "list", "--limit", "50"])

    assert restored.exit_code == 0, restored.output
    assert restored.stdout == original.stdout
    assert "cadastro" in restored.stdout
    assert "1/0/0" in restored.stdout


def test_restore_verification_detects_changed_file(
    history_context: ProjectContext,
    clock_now: list[datetime],
    fake_clock: Callable[[], datetime],
    tmp_path: Path,
) -> None:
    store = _populate(history_context, clock_now, fake_clock, tmp_path)
    result = store.backup(tmp_path / "copia.zip")
    restored = tmp_path / "restaurado"
    with zipfile.ZipFile(result.archive) as opened:
        opened.extractall(restored)
    evidence = next(restored.rglob("step-01.png"))

    evidence.write_bytes(b"alterado")

    assert verify_restored_backup(restored) == [
        f"hash ou tamanho diferente: {evidence.relative_to(restored).as_posix()}"
    ]
