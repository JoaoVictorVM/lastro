from collections.abc import Callable
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from lastro.cli import demo_cmd
from lastro.cli.app import app
from lastro.cli.state import CliState, get_state
from lastro.cli.stubs import STUB_COMMANDS


def test_help_lists_stub_commands(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for name in STUB_COMMANDS:
        assert name in result.stdout


@pytest.mark.parametrize("name", list(STUB_COMMANDS))
def test_stub_commands_report_not_implemented(runner: CliRunner, name: str) -> None:
    result = runner.invoke(app, [name, "qualquer", "--opcao-futura", "valor"])

    assert result.exit_code == 1
    assert f"{name}: não implementado nesta versão" in result.stderr


def _app_with_config_command(executed: list[str]) -> typer.Typer:
    test_app = typer.Typer()

    @test_app.callback()
    def root(ctx: typer.Context) -> None:
        ctx.obj = CliState()

    @test_app.command()
    def needs_config(ctx: typer.Context) -> None:
        project = get_state(ctx).require_project()
        executed.append(project.profile_name)

    return test_app


def test_require_project_blocks_command_with_invalid_config(
    runner: CliRunner, write_config: Callable[..., Path], profile_toml: Callable[..., str]
) -> None:
    write_config(profile_toml("homolog", extra='editable_fields = { employee = "phone" }'))
    executed: list[str] = []

    result = runner.invoke(_app_with_config_command(executed), ["needs-config"])

    assert result.exit_code == 2
    assert executed == []
    assert (
        "lastro.toml › profiles.homolog.editable_fields.employee: "
        "expected list of field names, got string"
    ) in result.stderr
    assert "nenhum comando foi executado" in result.stderr


def test_require_project_runs_command_with_valid_config(
    runner: CliRunner, write_config: Callable[..., Path], profile_toml: Callable[..., str]
) -> None:
    write_config(profile_toml("homolog"))
    executed: list[str] = []

    result = runner.invoke(_app_with_config_command(executed), ["needs-config"])

    assert result.exit_code == 0, result.output
    assert executed == ["homolog"]


ALL_COMMANDS = ["init", "doctor", "demo", *STUB_COMMANDS]


def test_help_lists_all_eleven_commands(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert len(ALL_COMMANDS) == 11
    listed = {line.strip("│| ").split(" ")[0] for line in result.stdout.splitlines()}
    assert set(ALL_COMMANDS) <= listed


def test_invalid_config_blocks_commands(
    runner: CliRunner,
    write_config: Callable[..., Path],
    profile_toml: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(profile_toml("homolog", extra='editable_fields = { employee = "phone" }'))
    called: list[str] = []
    monkeypatch.setattr(demo_cmd, "runner_loader", lambda: called.append("loaded"))

    result = runner.invoke(app, ["demo", "up"])

    assert result.exit_code == 2
    assert called == []
    assert (
        "lastro.toml › profiles.homolog.editable_fields.employee: "
        "expected list of field names, got string"
    ) in result.stderr
    assert "nenhum comando foi executado" in result.stderr


def test_version_and_init_work_with_invalid_config(
    runner: CliRunner,
    write_config: Callable[..., Path],
    workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = write_config("profiles = 1\n", workdir / "outro")
    project = workdir / "novo"
    project.mkdir()
    version = runner.invoke(app, ["--config", str(bad), "--version"])
    assert version.exit_code == 0

    monkeypatch.chdir(project)
    result = runner.invoke(
        app,
        [
            "--config",
            str(bad),
            "init",
            "--name",
            "homolog",
            "--target-url",
            "https://alvo.example.test",
            "--login-url",
            "https://alvo.example.test/login",
            "--tenant",
            "acme",
        ],
    )

    assert result.exit_code == 0, result.output
    assert (project / "lastro.toml").is_file()
    assert "nenhum comando foi executado" not in result.output
