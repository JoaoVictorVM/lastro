import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lastro.doctor.checks import DoctorEnvironment
from lastro.paths import is_inside_git_tree


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
    for proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(proxy_var, raising=False)
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


PERMISSION_FILE = Path(".claude") / "settings.json"


@pytest.fixture
def doctor_env() -> Callable[..., DoctorEnvironment]:
    """Ambiente simulado em que todos os checks externos passam, salvo o que for sobrescrito."""

    def make(**overrides: object) -> DoctorEnvironment:
        values: dict[str, object] = {
            "python_version": (3, 12, 4),
            "which": lambda name: f"/usr/bin/{name}",
            "playwright_installed": lambda: True,
            "chromium_installed": lambda: True,
            "http_status": lambda url, timeout: 200,
            "git_check_ignore": lambda path: True,
            "environ": os.environ,
        }
        values.update(overrides)
        return DoctorEnvironment(**values)  # type: ignore[arg-type]

    return make


@pytest.fixture
def healthy_project(
    workdir: Path, write_config: Callable[..., Path], profile_toml: Callable[..., str]
) -> Path:
    """Projeto válido: .env preenchido, permissão do executor e root fora do Git."""
    if is_inside_git_tree(workdir):
        pytest.skip("diretório temporário está dentro de um repositório Git")
    root = workdir.parent / "private-root"
    write_config(f"private_data_root = '{root.as_posix()}'\n" + profile_toml("homolog"))
    (workdir / ".env").write_text(
        "HOMOLOG_USERNAME=usuario\nHOMOLOG_PASSWORD=senha-de-teste\n", encoding="utf-8"
    )
    (workdir / PERMISSION_FILE).parent.mkdir()
    (workdir / PERMISSION_FILE).write_text("{}", encoding="utf-8")
    return workdir
