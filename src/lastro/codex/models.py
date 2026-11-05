"""Contratos internos do adapter; nenhum deles carrega segredo."""

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict

MINIMUM_VERSION = "0.166.0"
SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
HEX64 = re.compile(r"[0-9a-f]{64}")


def parse_version(text: str) -> tuple[int, int, int] | None:
    """Só versões estáveis contam: pré-release não comprova as capacidades exigidas."""
    match = SEMVER.fullmatch(text)
    if match is None:
        return None
    return int(match[1]), int(match[2]), int(match[3])


def _semver(value: str) -> str:
    if parse_version(value) is None:
        raise ValueError("expected a stable SemVer version")
    return value


def _hex64(value: str) -> str:
    if HEX64.fullmatch(value) is None:
        raise ValueError("expected a lowercase SHA-256")
    return value


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


Version = Annotated[str, AfterValidator(_semver)]
Sha256 = Annotated[str, AfterValidator(_hex64)]
Text = Annotated[str, AfterValidator(_not_blank)]


class LaunchMode(StrEnum):
    INTERACTIVE = "interactive"
    HEADLESS = "headless"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class AdapterManifest(_Strict):
    """Conteúdo de .codex/lastro.json: prova integridade local, não autenticidade."""

    schema_version: Literal[1]
    executor: Literal["codex"]
    minimum_version: Version
    config_sha256: Sha256
    instructions_sha256: Sha256


class LaunchPlan(_Strict):
    """Entrada validada do supervisor; não é um comando recebido do usuário."""

    executor: Literal["codex"]
    executor_version: Version
    auth_method: Literal["chatgpt"]
    mode: LaunchMode
    run_id: Text
    profile: Text
    project_dir: Text
    config_file: Text
    skill_bundle_sha256: Sha256


@dataclass(frozen=True)
class CodexInstall:
    """Comando absoluto já resolvido, sem passar por shell, e versão detectada."""

    command: tuple[str, ...]
    version: str


@dataclass(frozen=True)
class CodexResult:
    """Resultado normalizado: status e motivo vêm do histórico, nunca da mensagem do modelo."""

    run_id: str
    executor: str
    executor_version: str
    status: str
    reason: str | None
    exit_code: int
    report_kind: str | None
    report_path: Path | None = None
    diagnostic: str | None = None
    codex_exit_code: int | None = None

    def document(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "executor": self.executor,
            "executor_version": self.executor_version,
            "status": self.status,
            "reason": self.reason,
            "exit_code": self.exit_code,
            "report_kind": self.report_kind,
            "report_path": str(self.report_path) if self.report_path else None,
            "diagnostic": self.diagnostic,
            "codex_exit_code": self.codex_exit_code,
        }
