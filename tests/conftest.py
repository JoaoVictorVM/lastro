import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Isola diretório atual, home e diretórios de dados do usuário em uma pasta temporária."""
    snapshot = dict(os.environ)
    home = tmp_path / "home"
    workdir = tmp_path / "work"
    home.mkdir()
    workdir.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.chdir(workdir)
    yield workdir
    os.environ.clear()
    os.environ.update(snapshot)


@pytest.fixture
def workdir(isolated_env: Path) -> Path:
    return isolated_env


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()
