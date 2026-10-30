import json
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lastro.cli.app import app
from lastro.config.loader import ProjectContext
from lastro.history import ArtifactType, RunStatus, TextMission, open_history
from lastro.history import backup as backup_module


@pytest.fixture
def run_id(history_context: ProjectContext) -> str:
    store = open_history(history_context)
    run_id = store.create_run(history_context, TextMission("Explore")).record.id
    store.register_artifact(run_id, "evidence/a.png", ArtifactType.SCREENSHOT, content=b"png")
    store.finish_run(run_id, RunStatus.COMPLETED)
    return run_id


def test_backup_to_explicit_file_contains_database_runs_and_manifest(
    runner: CliRunner, history_context: ProjectContext, run_id: str, tmp_path: Path
) -> None:
    target = tmp_path / "copia.zip"

    result = runner.invoke(app, ["backup", str(target)])

    assert result.exit_code == 0, result.output
    assert f"Backup criado: {target}" in result.stdout
    project_id = history_context.require_project_id()
    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
    assert {
        "manifest.json",
        f"{project_id}/history.sqlite",
        f"{project_id}/runs/{run_id}/evidence/",
        f"{project_id}/runs/{run_id}/evidence/a.png",
    } <= names
    assert not any(name.endswith(("-wal", "-shm", "active-run.json")) for name in names)
    assert {entry["path"] for entry in manifest["files"]} == {
        f"{project_id}/history.sqlite",
        f"{project_id}/runs/{run_id}/evidence/a.png",
    }
    assert list(tmp_path.glob(".*")) == []


def test_backup_never_overwrites_existing_destination(
    runner: CliRunner, history_context: ProjectContext, run_id: str, tmp_path: Path
) -> None:
    target = tmp_path / "copia.zip"
    target.write_bytes(b"anterior")

    result = runner.invoke(app, ["backup", str(target)])

    assert result.exit_code == 1
    assert "o destino já existe" in result.stderr
    assert target.read_bytes() == b"anterior"


@pytest.mark.parametrize("relative", ["inexistente/copia.zip", "INSIDE"])
def test_backup_rejects_invalid_destination(
    runner: CliRunner,
    history_context: ProjectContext,
    run_id: str,
    tmp_path: Path,
    relative: str,
) -> None:
    target = (
        history_context.project_data_dir / "copia.zip"
        if relative == "INSIDE"
        else tmp_path / relative
    )

    result = runner.invoke(app, ["backup", str(target)])

    assert result.exit_code == 1
    assert "Backup não criado" in result.stderr
    assert not target.exists()


def test_failed_backup_publishes_nothing(
    runner: CliRunner,
    history_context: ProjectContext,
    run_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "backups"
    destination.mkdir()
    original = backup_module._add_file  # pyright: ignore[reportPrivateUsage]
    calls: list[str] = []

    def failing(archive: zipfile.ZipFile, source: Path, name: str) -> backup_module.ManifestEntry:
        calls.append(name)
        if len(calls) > 1:
            raise OSError("falha de escrita simulada")
        return original(archive, source, name)

    monkeypatch.setattr(backup_module, "_add_file", failing)

    result = runner.invoke(app, ["backup", str(destination)])

    assert result.exit_code == 1
    assert list(destination.iterdir()) == []
