"""Configuração opcional da fonte de código: allowlist, base e tetos que só podem diminuir."""

import os
import re
from pathlib import Path
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from pydantic_core import PydanticCustomError

DEFAULT_REPO_PATH_ENV = "SOURCE_REPO_PATH"
MAX_CANDIDATES = 500
MAX_TOTAL_BYTES = 20_000_000
MAX_DIFF_CHARACTERS = 200_000
MAX_SUMMARY_CHARACTERS = 12_000
MAX_FILE_BYTES = 1_000_000

_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
# Nome de ref aceito: sem espaços, sem sintaxe de revisão (~ ^ : ? * [ \ ..) e sem hífen inicial.
_REF = re.compile(r"^(?!-)(?!.*\.\.)(?!.*//)(?!.*@\{)[A-Za-z0-9._/+-]{1,200}(?<![./])$")


class SourceConfigError(Exception):
    """Fonte de código indisponível ou configurada de forma insegura."""


def is_valid_ref(value: str) -> bool:
    return _REF.match(value) is not None and not value.endswith(".lock")


def _ref(value: str) -> str:
    if not is_valid_ref(value):
        raise PydanticCustomError(
            "lastro_git_ref", "expected a plain git ref name (e.g. origin/main), no revision syntax"
        )
    return value


def _env_name(value: str) -> str:
    if not _ENV_NAME.match(value):
        raise PydanticCustomError(
            "lastro_env_var_name", "expected environment variable name matching [A-Z_][A-Z0-9_]*"
        )
    return value


def is_safe_glob(pattern: str) -> bool:
    """Glob POSIX relativo à raiz, sem drive, UNC, ADS, NUL, barra invertida ou componente '..'."""
    if not pattern or pattern.startswith("/") or "\\" in pattern or "\x00" in pattern:
        return False
    if ":" in pattern:
        return False
    return all(part not in ("", ".", "..") for part in pattern.split("/"))


def _globs(values: list[str]) -> list[str]:
    invalid = [value for value in values if not is_safe_glob(value)]
    if invalid:
        raise PydanticCustomError(
            "lastro_source_glob",
            "expected relative POSIX globs without '..', drive or absolute paths "
            "(invalid: {patterns})",
            {"patterns": ", ".join(invalid)},
        )
    return values


type RefName = Annotated[str, AfterValidator(_ref)]


class SourceSettings(BaseModel):
    """Seção [source]; sem allowlist explícita, --source é recusado."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    repo_path_env: Annotated[str, AfterValidator(_env_name)] = DEFAULT_REPO_PATH_ENV
    allow_paths: Annotated[list[str], AfterValidator(_globs)] = Field(
        default_factory=list[str], max_length=100
    )
    base_ref: RefName | None = None
    max_candidates: int = Field(default=MAX_CANDIDATES, ge=1, le=MAX_CANDIDATES)
    max_total_bytes: int = Field(default=MAX_TOTAL_BYTES, ge=1, le=MAX_TOTAL_BYTES)
    max_diff_characters: int = Field(default=MAX_DIFF_CHARACTERS, ge=1, le=MAX_DIFF_CHARACTERS)
    max_summary_characters: int = Field(
        default=MAX_SUMMARY_CHARACTERS, ge=1, le=MAX_SUMMARY_CHARACTERS
    )

    @property
    def enabled(self) -> bool:
        return bool(self.allow_paths)


def resolve_source_root(settings: SourceSettings) -> Path:
    """Raiz indicada pela variável de ambiente; o caminho nunca é exposto ao modelo."""
    if not settings.enabled:
        raise SourceConfigError(
            "--source requires [source] allow_paths in lastro.toml (explicit allowlist)."
        )
    raw = os.environ.get(settings.repo_path_env, "").strip()
    if not raw:
        raise SourceConfigError(
            f"--source requires the {settings.repo_path_env} environment variable."
        )
    path = Path(raw)
    if not path.is_absolute():
        raise SourceConfigError(f"{settings.repo_path_env} must be an absolute path.")
    if not path.is_dir():
        raise SourceConfigError(f"{settings.repo_path_env} does not point to a directory.")
    return path
