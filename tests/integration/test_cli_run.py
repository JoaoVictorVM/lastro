import os
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lastro.cli.app import app
from lastro.config.loader import MISSING_PROJECT_ID_MESSAGE, ProjectContext
from lastro.history import MissionSource, RunStatus, open_history

MISSION_SOURCE_ERROR = "Informe exatamente uma fonte de missão"


def _finish_active(context: ProjectContext) -> None:
    store = open_history(context)
    active = store.get_active_run()
    if active is not None:
        store.finish_run(active.id, RunStatus.COMPLETED)


def test_text_and_file_mission_flags_are_mutually_exclusive(
    runner: CliRunner, history_context: ProjectContext, workdir: Path
) -> None:
    mission = workdir / "missions" / "employee.md"
    mission.parent.mkdir()
    mission.write_text("Cadastrar funcionário", encoding="utf-8")
    base = ["--profile", "homolog", "run", "start"]

    both = runner.invoke(
        app, [*base, "--mission", "texto", "--mission-file", "missions/employee.md"]
    )
    neither = runner.invoke(app, base)

    for result in (both, neither):
        assert result.exit_code == 2
        assert MISSION_SOURCE_ERROR in result.stderr
    assert not history_context.project_data_dir.exists()

    text = runner.invoke(app, [*base, "--mission", "Explore o cadastro"])
    assert text.exit_code == 0, text.output
    _finish_active(history_context)
    from_file = runner.invoke(app, [*base, "--mission-file", "missions/employee.md"])
    assert from_file.exit_code == 0, from_file.output

    runs = {summary.record.mission_source for summary in open_history(history_context).list_runs()}
    assert runs == {MissionSource.TEXT, MissionSource.FILE}


def test_text_mission_longer_than_limit_is_rejected(
    runner: CliRunner, history_context: ProjectContext
) -> None:
    result = runner.invoke(app, ["--profile", "homolog", "run", "start", "--mission", "x" * 10_001])

    assert result.exit_code == 1
    assert "10000 caracteres" in result.stderr
    assert open_history(history_context).list_runs() == []


def test_second_run_while_one_is_active_is_refused(
    runner: CliRunner, history_context: ProjectContext
) -> None:
    args = ["--profile", "homolog", "run", "start", "--mission", "Explore"]
    assert runner.invoke(app, args).exit_code == 0

    result = runner.invoke(app, args)

    assert result.exit_code == 1
    assert "já existe uma run ativa" in result.stderr
    assert len(open_history(history_context).list_runs()) == 1


@pytest.mark.parametrize(
    "command",
    [
        ["--profile", "homolog", "run", "start", "--mission", "Explore"],
        ["history", "list"],
        ["tail"],
        ["backup", "backup.zip"],
    ],
)
def test_history_command_without_project_id_fails_before_side_effects(
    runner: CliRunner,
    write_config: Callable[..., Path],
    profile_toml: Callable[..., str],
    workdir: Path,
    command: list[str],
) -> None:
    root = workdir.parent / "private-root"
    config = write_config(f"private_data_root = '{root.as_posix()}'\n" + profile_toml("homolog"))
    before = config.read_bytes()
    entries = sorted(workdir.iterdir())

    result = runner.invoke(app, command)

    assert result.exit_code == 2
    assert MISSING_PROJECT_ID_MESSAGE in result.stderr
    assert (
        MISSING_PROJECT_ID_MESSAGE
        == "Este projeto ainda não possui project_id. Execute 'lastro init' para migrar o "
        "lastro.toml."
    )
    assert not root.exists()
    assert sorted(workdir.iterdir()) == entries
    assert config.read_bytes() == before


def test_run_start_never_writes_lastro_toml(
    runner: CliRunner, history_context: ProjectContext
) -> None:
    config = history_context.config_path
    os.utime(config, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    before = config.stat()
    content = config.read_bytes()

    result = runner.invoke(app, ["--profile", "homolog", "run", "start", "--mission", "Explore"])

    assert result.exit_code == 0, result.output
    after = config.stat()
    assert config.read_bytes() == content
    assert after.st_mtime_ns == before.st_mtime_ns
    assert (after.st_ino, after.st_dev) == (before.st_ino, before.st_dev)
    assert sorted(path.name for path in config.parent.iterdir() if path.name.startswith(".")) == []


def test_run_start_prints_id_and_folder_for_selected_profile(
    runner: CliRunner, history_context_for: Callable[[str], ProjectContext]
) -> None:
    local = history_context_for("local")

    result = runner.invoke(app, ["--profile", "local", "run", "start", "--mission", "Busca"])

    assert result.exit_code == 0, result.output
    (summary,) = open_history(local).list_runs()
    assert summary.record.profile == "local"
    assert result.stdout.splitlines() == [
        f"Run: {summary.record.id}",
        f"Pasta: {local.project_data_dir / 'runs' / summary.record.id}",
    ]
