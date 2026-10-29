import os
from collections.abc import Callable, Iterator
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


def profile_block(name: str = "homolog", extra: str = "", credentials_prefix: str = "") -> str:
    prefix = credentials_prefix or name.upper().replace("-", "_")
    return f"""
[profiles.{name}]
target_url = "https://{name}.example.test"
login_url = "https://{name}.example.test/login"
tenant_id = "acme"
allowed_entities = ["employee"]
{extra}

[profiles.{name}.credentials]
username_env = "{prefix}_USERNAME"
password_env = "{prefix}_PASSWORD"
"""


@pytest.fixture
def write_config(workdir: Path) -> Callable[..., Path]:
    def write(text: str, directory: Path | None = None) -> Path:
        target_dir = directory or workdir
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / "lastro.toml"
        path.write_text(text, encoding="utf-8")
        return path

    return write


@pytest.fixture
def profile_toml() -> Callable[..., str]:
    return profile_block
