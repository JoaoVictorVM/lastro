"""Schemas da configuração do projeto (lastro.toml)."""

import itertools
import re
import uuid
from typing import Annotated, Literal, Self
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
JSON_PATH_PATTERN = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$")
DEFAULT_AUTH_TIMEOUT_SECONDS = 15
AUTH_TIMEOUT_MIN = 1
AUTH_TIMEOUT_MAX = 60
DEFAULT_API_VERIFICATION_ENDPOINT = "/api/me"
DEFAULT_API_TENANT_JSON_PATH = "tenant.id"
# Ordem de preferência dos métodos de verificação de tenant: a lista configurada deve respeitá-la.
VERIFICATION_ORDER = ("api", "cookie", "jwt", "page")
ASYMMETRIC_JWT_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}
)


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


def _json_path(value: str) -> str:
    if not JSON_PATH_PATTERN.match(value):
        raise PydanticCustomError(
            "lastro_json_path", "expected dot-separated keys (e.g. tenant.id)"
        )
    return value


def _path_or_url(value: str) -> str:
    if value.startswith("/") or is_http_url(value):
        return value
    raise PydanticCustomError("lastro_path_or_url", "expected path starting with / or http(s) URL")


def _jwt_algorithms(value: list[str]) -> list[str]:
    if not value:
        raise PydanticCustomError("lastro_jwt_algorithms", "expected at least one algorithm")
    invalid = sorted(item for item in value if item not in ASYMMETRIC_JWT_ALGORITHMS)
    if invalid:
        raise PydanticCustomError(
            "lastro_jwt_algorithms",
            "expected asymmetric JWT algorithms ({allowed}), invalid: {names}",
            {"allowed": ", ".join(sorted(ASYMMETRIC_JWT_ALGORITHMS)), "names": ", ".join(invalid)},
        )
    return value


def origin_of(url: str) -> tuple[str, str, int | None]:
    """Origem (esquema, host, porta efetiva) usada nas comparações same-origin."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    default_port = {"http": 80, "https": 443}.get(scheme)
    return scheme, (parts.hostname or "").lower(), parts.port or default_port


def is_same_origin(url: str, base_url: str) -> bool:
    return origin_of(url) == origin_of(base_url)


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


class ApiVerification(BaseModel):
    """Tenant lido de um endpoint JSON autenticado, com o cookie jar da sessão."""

    model_config = _STRICT

    type: Literal["api"]
    endpoint: Annotated[str, AfterValidator(_path_or_url)]
    tenant_json_path: Annotated[str, AfterValidator(_json_path)]
    user_json_path: Annotated[str, AfterValidator(_json_path)] | None = None


class CookieVerification(BaseModel):
    model_config = _STRICT

    type: Literal["cookie"]
    cookie_name: NonEmptyText


class JwtVerification(BaseModel):
    model_config = _STRICT

    type: Literal["jwt"]
    cookie_name: NonEmptyText
    claim_path: Annotated[str, AfterValidator(_json_path)]
    jwks_url: Annotated[str, AfterValidator(_path_or_url)]
    issuer: NonEmptyText
    audience: NonEmptyText
    algorithms: Annotated[list[str], AfterValidator(_jwt_algorithms)]


class PageVerification(BaseModel):
    model_config = _STRICT

    type: Literal["page"]
    selector: NonEmptyText
    expected_text: NonEmptyText | None = None


Verification = Annotated[
    ApiVerification | CookieVerification | JwtVerification | PageVerification,
    Field(discriminator="type"),
]


def _default_verification() -> list[Verification]:
    return [
        ApiVerification(
            type="api",
            endpoint=DEFAULT_API_VERIFICATION_ENDPOINT,
            tenant_json_path=DEFAULT_API_TENANT_JSON_PATH,
        )
    ]


class AuthSettings(BaseModel):
    """Script de login e cadeia de verificação de tenant; os padrões funcionam com o demo."""

    model_config = _STRICT

    username_selector: NonEmptyText = '[name="username"]'
    password_selector: NonEmptyText = '[name="password"]'
    submit_selector: NonEmptyText = 'button[type="submit"]'
    tenant_selector: NonEmptyText | None = '[name="tenant"]'
    tenant_submit_selector: NonEmptyText | None = 'button[type="submit"]'
    timeout_seconds: int = Field(
        default=DEFAULT_AUTH_TIMEOUT_SECONDS, ge=AUTH_TIMEOUT_MIN, le=AUTH_TIMEOUT_MAX
    )
    verification: list[Verification] = Field(default_factory=_default_verification)

    @field_validator("verification")
    @classmethod
    def _preference_order(cls, value: list[Verification]) -> list[Verification]:
        ranks = [VERIFICATION_ORDER.index(item.type) for item in value]
        if any(later <= earlier for earlier, later in itertools.pairwise(ranks)):
            raise PydanticCustomError(
                "lastro_verification_order",
                "expected each method at most once, in the order {order}",
                {"order": ", ".join(VERIFICATION_ORDER)},
            )
        return value

    @model_validator(mode="after")
    def _tenant_submit_required(self) -> Self:
        if self.tenant_selector is not None and self.tenant_submit_selector is None:
            raise PydanticCustomError(
                "lastro_tenant_submit",
                "expected tenant_submit_selector when tenant_selector is set",
            )
        return self


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
    auth: AuthSettings = Field(default_factory=AuthSettings)

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

    @field_validator("auth")
    @classmethod
    def _auth_matches_profile(cls, value: AuthSettings, info: ValidationInfo) -> AuthSettings:
        # Perfil com entidades permitidas pode escrever; escrita exige tenant verificável.
        if info.data.get("allowed_entities") and not value.verification:
            raise PydanticCustomError(
                "lastro_verification_required",
                "expected at least one tenant verification method when allowed_entities is set",
            )
        target_url = info.data.get("target_url")
        if target_url is None:
            return value
        for item in value.verification:
            if (
                isinstance(item, ApiVerification)
                and is_http_url(item.endpoint)
                and not is_same_origin(item.endpoint, target_url)
            ):
                raise PydanticCustomError(
                    "lastro_same_origin",
                    "expected API verification endpoint on the target_url origin",
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
