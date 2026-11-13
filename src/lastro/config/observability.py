"""Observabilidade opt-in por perfil: destino Langfuse congelado na run e chaves fora do arquivo.

Nada aqui faz rede nem importa o SDK; o bloco ausente equivale a `enabled=false`, e variáveis
globais `OTEL_*` ou chaves presentes no ambiente nunca habilitam a exportação.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Annotated, Self
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, model_validator
from pydantic_core import PydanticCustomError

MAPPING_VERSION = 1
OTLP_TRACES_PATH = "/api/public/otel/v1/traces"
DEFAULT_PUBLIC_KEY_ENV = "LANGFUSE_PUBLIC_KEY"
DEFAULT_SECRET_KEY_ENV = "LANGFUSE_SECRET_KEY"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_PROJECT_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_LABEL = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    return scheme, (parts.hostname or "").lower(), parts.port or {"http": 80, "https": 443}[scheme]


def _destination_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise PydanticCustomError("lastro_obs_url", "expected absolute http or https URL")
    if parts.username is not None or parts.password is not None:
        raise PydanticCustomError("lastro_obs_url", "expected URL without user information")
    if parts.query or parts.fragment or "?" in value or "#" in value:
        raise PydanticCustomError("lastro_obs_url", "expected URL without query or fragment")
    # HTTP só no loopback literal: Basic Auth nunca trafega em claro por rede.
    if parts.scheme == "http" and parts.hostname.lower() not in LOOPBACK_HOSTS:
        raise PydanticCustomError(
            "lastro_obs_https", "expected https (http only on localhost, 127.0.0.1 or [::1])"
        )
    return value


def _env_name(value: str) -> str:
    if not _ENV_NAME.match(value):
        raise PydanticCustomError(
            "lastro_env_var_name", "expected environment variable name matching [A-Z_][A-Z0-9_]*"
        )
    return value


def _project_segment(value: str) -> str:
    if not _PROJECT_SEGMENT.match(value):
        raise PydanticCustomError(
            "lastro_obs_project", "expected 1 to 128 characters among [A-Za-z0-9_-]"
        )
    return value


DestinationUrl = Annotated[str, AfterValidator(_destination_url)]
EnvName = Annotated[str, AfterValidator(_env_name)]


class ObservabilitySettings(BaseModel):
    """Tabela `profiles.<nome>.observability`; campos extras e tipos frouxos falham no load."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    enabled: bool = False
    endpoint: DestinationUrl | None = None
    ui_url: DestinationUrl | None = None
    langfuse_project_id: Annotated[str, AfterValidator(_project_segment)] | None = None
    public_key_env: EnvName = DEFAULT_PUBLIC_KEY_ENV
    secret_key_env: EnvName = DEFAULT_SECRET_KEY_ENV

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.public_key_env == self.secret_key_env:
            raise PydanticCustomError(
                "lastro_obs_keys", "expected public_key_env different from secret_key_env"
            )
        if self.endpoint is not None and not urlsplit(self.endpoint).path.rstrip("/").endswith(
            OTLP_TRACES_PATH
        ):
            raise PydanticCustomError(
                "lastro_obs_endpoint",
                "expected endpoint path ending in {path}",
                {"path": OTLP_TRACES_PATH},
            )
        if not self.enabled:
            return self
        missing = [
            name
            for name, value in (
                ("endpoint", self.endpoint),
                ("ui_url", self.ui_url),
                ("langfuse_project_id", self.langfuse_project_id),
            )
            if value is None
        ]
        if missing:
            raise PydanticCustomError(
                "lastro_obs_required",
                "expected {names} when observability is enabled",
                {"names": ", ".join(missing)},
            )
        assert self.endpoint is not None and self.ui_url is not None
        if _origin(self.endpoint) != _origin(self.ui_url):
            raise PydanticCustomError(
                "lastro_obs_origin", "expected endpoint and ui_url on the same origin"
            )
        return self

    def destination(self) -> "Destination | None":
        if not self.enabled:
            return None
        assert self.endpoint is not None and self.ui_url is not None
        assert self.langfuse_project_id is not None
        return Destination(
            endpoint=self.endpoint,
            ui_url=self.ui_url,
            langfuse_project_id=self.langfuse_project_id,
        )


@dataclass(frozen=True)
class Destination:
    """Destino do operador, congelado na criação da run; não contém segredo."""

    endpoint: str
    ui_url: str
    langfuse_project_id: str

    def as_json(self) -> dict[str, str]:
        return {
            "endpoint": self.endpoint,
            "langfuse_project_id": self.langfuse_project_id,
            "ui_url": self.ui_url,
        }

    @property
    def sha256(self) -> str:
        canonical = json.dumps(self.as_json(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class EvaluationContextError(ValueError):
    code = "obs_config_invalid"


@dataclass(frozen=True)
class EvaluationTraceContext:
    """Etiquetas de suíte e condição recebidas só de chamador confiável (runner de avaliação)."""

    suite_id: str
    condition: str

    def __post_init__(self) -> None:
        for name, value in (("suite_id", self.suite_id), ("condition", self.condition)):
            if not _LABEL.match(value):
                raise EvaluationContextError(
                    f"{name} must have 1 to 128 characters among [A-Za-z0-9_.-]"
                )

    @classmethod
    def from_columns(cls, suite_id: str | None, condition: str | None) -> "Self | None":
        if suite_id is None or condition is None:
            return None
        return cls(suite_id=suite_id, condition=condition)


@dataclass(frozen=True)
class ObservabilityContextInput:
    """Contexto congelado gravado junto com a run e o evento run_started."""

    destination: Destination
    evaluation: EvaluationTraceContext | None = None
