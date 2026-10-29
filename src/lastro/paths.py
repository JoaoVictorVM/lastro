"""Private data root: onde ficam os dados privados do projeto, fora do repositório."""

import os
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path

APP_DIRNAME = "lastro"
PROFILES_DIRNAME = "profiles"
DEMO_DIRNAME = "demo"


def default_private_data_root(
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    platform = platform if platform is not None else sys.platform
    env = env if env is not None else os.environ
    home = home if home is not None else Path(os.path.expanduser("~"))
    if platform.startswith("win"):
        local_appdata = env.get("LOCALAPPDATA")
        base = Path(local_appdata) if local_appdata else home / "AppData" / "Local"
        return base / APP_DIRNAME
    return home / ".local" / "share" / APP_DIRNAME


def resolve_private_data_root(override: str | None, base_dir: Path | None = None) -> Path:
    """Usa a sobrescrita do lastro.toml (relativa ao diretório dele) ou o padrão da plataforma."""
    if not override:
        return default_private_data_root()
    path = Path(os.path.expanduser(override))
    if not path.is_absolute() and base_dir is not None:
        path = base_dir / path
    return path


def is_inside_git_tree(path: Path) -> bool:
    # O caminho pode ainda não existir; os ancestrais existentes decidem.
    current = Path(os.path.abspath(path))
    return any((candidate / ".git").exists() for candidate in (current, *current.parents))


def ensure_writable(path: Path) -> None:
    """Cria o diretório se preciso e prova a escrita gravando e removendo um arquivo de teste."""
    path.mkdir(parents=True, exist_ok=True)
    probe = path / f".lastro-write-test-{uuid.uuid4().hex}"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()


def profile_data_dir(private_data_root: Path, profile_name: str) -> Path:
    return private_data_root / PROFILES_DIRNAME / profile_name


def demo_state_dir(private_data_root: Path) -> Path:
    return private_data_root / DEMO_DIRNAME
