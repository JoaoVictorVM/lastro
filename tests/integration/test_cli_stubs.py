from collections.abc import Callable
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

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
