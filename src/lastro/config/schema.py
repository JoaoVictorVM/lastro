"""Schemas da configuração do projeto (lastro.toml)."""

import re
import uuid
from typing import Annotated, Self
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

DEFAULT_TIMEZONE = "America/Sao_Paulo"
DEFAULT_EXECUTOR = "claude-code"
DEFAULT_DEMO_PORT = 8765
DEMO_PORT_MIN = 1024
DEMO_PORT_MAX = 65535
MAX_PROFILES = 10
PROFILE_NAME_MAX_LENGTH = 40

PROFILE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
ENV_VAR_NAME_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")
HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"})


def is_valid_profile_name(value: str) -> bool:
    return len(value) <= PROFILE_NAME_MAX_LENGTH and PROFILE_NAME_PATTERN.match(value) is not None


def is_http_url(value: str) -> bool:
    parts = urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def _http_url(value: str) -> str:
    if not is_http_url(value):
        raise PydanticCustomError("lastro_http_url", "expected http or https URL with host")
    return value


def _env_var_name(value: str) -> str:
    if not ENV_VAR_NAME_PATTERN.match(value):
        raise PydanticCustomError(
            "lastro_env_var_name", "expected environment variable name matching [A-Z_][A-Z0-9_]*"
        )
    return value


def is_canonical_project_id(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def new_project_id() -> str:
    return str(uuid.uuid4())


def _project_id(value: str) -> str:
    if not is_canonical_project_id(value):
        raise PydanticCustomError("lastro_project_id", "expected UUID in canonical lowercase form")
    return value


def _non_empty(value: str) -> str:
    if not value.strip():
        raise PydanticCustomError("lastro_non_empty", "expected non-empty text")
    return value


def _http_method(value: str) -> str:
    if value not in HTTP_METHODS:
        raise PydanticCustomError(
            "lastro_http_method",
            "expected uppercase HTTP method (GET, POST, PUT, PATCH, DELETE...)",
        )
    return value


def _version_endpoint(value: str) -> str:
    if value.startswith("/") or is_http_url(value):
        return value
    raise PydanticCustomError(
        "lastro_version_endpoint", "expected path starting with / or http(s) URL"
    )


def _scalar_values(value: dict[str, object]) -> dict[str, object]:
    # As chaves definitivas dos limites de run ainda não existem; só tipos básicos são aceitos.
    invalid = sorted(key for key, item in value.items() if not isinstance(item, int | float | str))
    if invalid:
        raise PydanticCustomError(
            "lastro_run_limits",
            "expected table of numbers, booleans or text (invalid keys: {keys})",
            {"keys": ", ".join(invalid)},
        )
    return value


HttpUrlText = Annotated[str, AfterValidator(_http_url)]
EnvVarName = Annotated[str, AfterValidator(_env_var_name)]
NonEmptyText = Annotated[str, AfterValidator(_non_empty)]
HttpMethod = Annotated[str, AfterValidator(_http_method)]

_STRICT = ConfigDict(extra="forbid", strict=True)


class Credentials(BaseModel):
    """Referências por nome de variável de ambiente; os valores nunca ficam no arquivo."""

    model_config = _STRICT

    username_env: EnvVarName
    password_env: EnvVarName

    def variable_names(self) -> list[str]:
        return [self.username_env, self.password_env]


class Profile(BaseModel):
    model_config = _STRICT

    name: str = Field(default="", exclude=True)
    target_url: HttpUrlText
    login_url: HttpUrlText
    tenant_id: NonEmptyText
    tenant_display_name: str | None = None
    credentials: Credentials
    version_endpoint: Annotated[str, AfterValidator(_version_endpoint)] | None = None
    allowed_entities: list[NonEmptyText]
    editable_fields: dict[str, list[NonEmptyText]] = Field(default_factory=dict[str, list[str]])
    denied_methods: list[HttpMethod] = Field(default_factory=lambda: ["DELETE"])
    denied_endpoint_patterns: list[NonEmptyText] = Field(default_factory=list[str])
    run_limits: Annotated[dict[str, object], AfterValidator(_scalar_values)] = Field(
        default_factory=dict[str, object]
    )

    @field_validator("editable_fields")
    @classmethod
    def _entities_must_be_allowed(
        cls, value: dict[str, list[str]], info: ValidationInfo
    ) -> dict[str, list[str]]:
        allowed = info.data.get("allowed_entities")
        if allowed is None:
            return value
        unknown = sorted(entity for entity in value if entity not in allowed)
        if unknown:
            raise PydanticCustomError(
                "lastro_entity_not_allowed",
                "expected only entities listed in allowed_entities (not allowed: {entities})",
                {"entities": ", ".join(unknown)},
            )
        return value

    @model_validator(mode="after")
    def _default_display_name(self) -> Self:
        if self.tenant_display_name is None:
            self.tenant_display_name = self.tenant_id
        return self

    def credential_variable_names(self) -> list[str]:
        return self.credentials.variable_names()


class DemoSettings(BaseModel):
    model_config = _STRICT

    port: int = Field(default=DEFAULT_DEMO_PORT, ge=DEMO_PORT_MIN, le=DEMO_PORT_MAX)


class LastroConfig(BaseModel):
    model_config = _STRICT

    # Opcional no schema para que projetos anteriores ainda carreguem e possam ser migrados por
    # lastro init; os comandos do histórico exigem o valor antes de qualquer efeito.
    project_id: Annotated[str, AfterValidator(_project_id)] | None = None
    timezone: str = DEFAULT_TIMEZONE
    private_data_root: str | None = None
    executor: NonEmptyText = DEFAULT_EXECUTOR
    demo: DemoSettings = Field(default_factory=DemoSettings)
    profiles: dict[str, Profile]

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise PydanticCustomError(
                "lastro_timezone", "expected IANA timezone name (e.g. America/Sao_Paulo)"
            ) from None
        return value

    @field_validator("profiles")
    @classmethod
    def _profiles_shape(cls, value: dict[str, Profile]) -> dict[str, Profile]:
        if not 1 <= len(value) <= MAX_PROFILES:
            raise PydanticCustomError(
                "lastro_profile_count",
                "expected between 1 and {maximum} profiles",
                {"maximum": MAX_PROFILES},
            )
        invalid = sorted(name for name in value if not is_valid_profile_name(name))
        if invalid:
            raise PydanticCustomError(
                "lastro_profile_name",
                "expected profile names matching [a-z0-9][a-z0-9_-]* (max {maximum} chars), "
                "invalid: {names}",
                {"maximum": PROFILE_NAME_MAX_LENGTH, "names": ", ".join(invalid)},
            )
        for name, profile in value.items():
            profile.name = name
        return value
