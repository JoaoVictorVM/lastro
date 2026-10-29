"""Lastro: agente de exploração de sistemas web com políticas e histórico auditável."""

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

DISTRIBUTION_NAME = "lastro"
UNKNOWN_VERSION = "0.0.0+unknown"


def _version_from_source_tree() -> str:
    # Checkout sem instalação: os metadados não existem, então a única fonte da versão é o
    # pyproject.toml do repositório.
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return UNKNOWN_VERSION
    project = data.get("project", {})
    if project.get("name") != DISTRIBUTION_NAME:
        return UNKNOWN_VERSION
    return str(project.get("version", UNKNOWN_VERSION))


def get_version() -> str:
    try:
        return version(DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return _version_from_source_tree()


__version__ = get_version()
