import pytest
from typer.testing import CliRunner

from lastro.cli.app import app
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
