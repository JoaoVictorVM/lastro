"""Descoberta e carregamento do lastro.toml, do .env e seleção do perfil de ambiente."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values
from pydantic import ValidationError

from lastro.config.errors import (
    ConfigError,
    display_path,
    format_toml_error,
    format_validation_error,
)
from lastro.config.schema import LastroConfig, Profile
from lastro.paths import profile_data_dir, project_data_dir, resolve_private_data_root

CONFIG_FILENAME = "lastro.toml"
ENV_FILENAME = ".env"
MISSING_PROJECT_ID_MESSAGE = (
    "Este projeto ainda não possui project_id. Execute 'lastro init' para migrar o lastro.toml."
)


class ConfigNotFoundError(ConfigError):
    pass


class ProfileSelectionError(ConfigError):
    pass


class MissingProjectIdError(ConfigError):
    def __init__(self) -> None:
        super().__init__([MISSING_PROJECT_ID_MESSAGE])


def find_config(start: Path) -> Path | None:
    current = start.resolve()
    for directory in (current, *current.parents):
        candidate = directory / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


def resolve_config_path(explicit: Path | None, cwd: Path | None = None) -> Path:
    cwd = cwd or Path.cwd()
    if explicit is not None:
        path = explicit if explicit.is_absolute() else cwd / explicit
        if not path.is_file():
            raise ConfigNotFoundError([f"{display_path(path, cwd)}: arquivo não encontrado"])
        return path
    found = find_config(cwd)
    if found is None:
        raise ConfigNotFoundError(
            [
                f"{CONFIG_FILENAME} não encontrado em {cwd} nem nos diretórios acima; "
                "rode lastro init para criar um"
            ]
        )
    return found


def load_config(path: Path, cwd: Path | None = None) -> LastroConfig:
    label = display_path(path, cwd)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError([format_toml_error(exc, label)]) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError([f"{label}: não foi possível ler o arquivo ({exc})"]) from None
    try:
        return LastroConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(format_validation_error(exc, label)) from None


def load_env_file(config_path: Path) -> list[str]:
    """Carrega o .env ao lado do lastro.toml sem sobrescrever variáveis já definidas no ambiente.

    Retorna os nomes das variáveis aplicadas a partir do arquivo.
    """
    env_path = config_path.parent / ENV_FILENAME
    if not env_path.is_file():
        return []
    applied: list[str] = []
    for name, value in dotenv_values(env_path).items():
        if value is None or name in os.environ:
            continue
        os.environ[name] = value
        applied.append(name)
    return applied


def select_profile(config: LastroConfig, name: str | None) -> Profile:
    available = ", ".join(config.profiles)
    if name is None:
        if len(config.profiles) == 1:
            return next(iter(config.profiles.values()))
        raise ProfileSelectionError(
            [f"vários perfis configurados; informe --profile <nome> (disponíveis: {available})"]
        )
    profile = config.profiles.get(name)
    if profile is None:
        raise ProfileSelectionError([f"perfil '{name}' não existe (disponíveis: {available})"])
    return profile


def select_profiles(config: LastroConfig, name: str | None) -> list[Profile]:
    """Perfil informado, ou todos quando nenhum foi pedido."""
    if name is None:
        return list(config.profiles.values())
    return [select_profile(config, name)]


@dataclass(frozen=True)
class LoadedConfig:
    path: Path
    config: LastroConfig

    @property
    def project_dir(self) -> Path:
        return self.path.parent

    @property
    def private_data_root(self) -> Path:
        return resolve_private_data_root(self.config.private_data_root, self.project_dir)

    @property
    def project_id(self) -> str | None:
        return self.config.project_id

    def require_project_id(self) -> str:
        if self.config.project_id is None:
            raise MissingProjectIdError()
        return self.config.project_id

    @property
    def project_data_dir(self) -> Path:
        """Pasta privada do projeto, comum a todos os perfis; exige project_id persistido."""
        return project_data_dir(self.private_data_root, self.require_project_id())


@dataclass(frozen=True)
class ProjectContext:
    """Configuração carregada com o perfil ativo resolvido, como as demais features consomem."""

    loaded: LoadedConfig
    profile: Profile

    @property
    def config(self) -> LastroConfig:
        return self.loaded.config

    @property
    def config_path(self) -> Path:
        return self.loaded.path

    @property
    def profile_name(self) -> str:
        return self.profile.name

    @property
    def private_data_root(self) -> Path:
        return self.loaded.private_data_root

    @property
    def profile_dir(self) -> Path:
        return profile_data_dir(self.private_data_root, self.profile.name)

    @property
    def project_id(self) -> str | None:
        return self.loaded.project_id

    def require_project_id(self) -> str:
        return self.loaded.require_project_id()

    @property
    def project_data_dir(self) -> Path:
        return self.loaded.project_data_dir

    @property
    def timezone(self) -> str:
        return self.config.timezone


def load_config_file(explicit: Path | None, cwd: Path | None = None) -> LoadedConfig:
    path = resolve_config_path(explicit, cwd)
    config = load_config(path, cwd)
    load_env_file(path)
    return LoadedConfig(path=path, config=config)


def load_project(
    explicit: Path | None = None, profile: str | None = None, cwd: Path | None = None
) -> ProjectContext:
    loaded = load_config_file(explicit, cwd)
    return ProjectContext(loaded=loaded, profile=select_profile(loaded.config, profile))
