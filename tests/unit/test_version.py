import platform
import re
import tomllib
from pathlib import Path

from typer.testing import CliRunner

import lastro
from lastro.cli.app import app

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def _pyproject_version() -> str:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]


def test_version_matches_pyproject() -> None:
    assert lastro.get_version() == _pyproject_version()
    assert lastro.__version__ == _pyproject_version()


def test_version_fallback_reads_source_tree() -> None:
    assert lastro._version_from_source_tree() == _pyproject_version()


def test_version_output_format(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    expected = (
        rf"^lastro {re.escape(_pyproject_version())} "
        rf"\(python {re.escape(platform.python_version())}, \S.*\)$"
    )
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 1
    assert re.match(expected, lines[0])
