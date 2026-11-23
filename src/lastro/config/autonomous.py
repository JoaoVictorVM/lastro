"""Configuração estrita do modo autônomo: limites, canais e filtros de eventos pós-deploy.

Webhooks, usuário e senha SMTP são sempre nomes de variáveis de ambiente; valores nunca ficam no
arquivo e nunca entram no snapshot da run.
"""

import hashlib
import json
import re
from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

from lastro.config.native import Usd

ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
CHANNEL_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
EMAIL = re.compile(r"^[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[^@\s,;<>\"']+$")
HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
DEFAULT_MAX_MINUTES = 30
DEFAULT_NATIVE_COST = "2.00"
MAX_CHANNELS = 10

_STRICT = ConfigDict(extra="forbid", strict=True, frozen=True)


def _env_name(value: str) -> str:
    if not ENV_NAME.fullmatch(value):
        raise PydanticCustomError(
            "lastro_env_reference",
            "expected an environment variable name (e.g. LASTRO_SLACK_WEBHOOK), never a value",
        )
    return value


def _channel_name(value: str) -> str:
    if not CHANNEL_NAME.fullmatch(value):
        raise PydanticCustomError("lastro_channel_name", "expected [a-z0-9][a-z0-9_-]{0,39}")
    return value


def _email(value: str) -> str:
    if not EMAIL.fullmatch(value):
        raise PydanticCustomError("lastro_email", "expected a plain e-mail address")
    return value


def _host(value: str) -> str:
    if not HOST.fullmatch(value) or value.startswith(("-", ".")):
        raise PydanticCustomError("lastro_smtp_host", "expected a host name without scheme")
    return value


EnvName = Annotated[str, AfterValidator(_env_name)]
ChannelName = Annotated[str, AfterValidator(_channel_name)]
Email = Annotated[str, AfterValidator(_email)]


class _Channel(BaseModel):
    model_config = _STRICT

    name: ChannelName

    def public(self) -> dict[str, Any]:
        raise NotImplementedError

    def fingerprint(self) -> str:
        """Hash da configuração redigida: identifica mudança de canal sem expor destino."""
        text = json.dumps(self.public(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SlackChannel(_Channel):
    type: Literal["slack"]
    webhook_env: EnvName

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type, "webhook_env": self.webhook_env}


class TeamsChannel(_Channel):
    type: Literal["teams"]
    webhook_env: EnvName

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type, "webhook_env": self.webhook_env}


class SmtpChannel(_Channel):
    type: Literal["smtp"]
    host: Annotated[str, AfterValidator(_host)]
    port: int = Field(default=587, ge=1, le=65535)
    starttls: bool = True
    # TLS implícito (porta 465); exatamente um dos dois modos precisa estar ativo.
    ssl: bool = False
    username_env: EnvName | None = None
    password_env: EnvName | None = None
    from_address: Email
    to: Annotated[list[Email], Field(min_length=1, max_length=20)]
    timeout_seconds: int = Field(default=20, ge=1, le=120)

    @model_validator(mode="after")
    def _tls_required(self) -> Self:
        if self.starttls == self.ssl:
            raise PydanticCustomError(
                "lastro_smtp_tls", "SMTP requires TLS: enable exactly one of starttls or ssl"
            )
        if (self.username_env is None) != (self.password_env is None):
            raise PydanticCustomError(
                "lastro_smtp_auth", "username_env and password_env must be set together"
            )
        return self

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "host": self.host,
            "port": self.port,
            "tls": "starttls" if self.starttls else "ssl",
            "recipients": len(self.to),
        }


Channel = Annotated[SlackChannel | TeamsChannel | SmtpChannel, Field(discriminator="type")]


class GithubTriggers(BaseModel):
    """Filtros dos eventos pós-deploy; o evento nunca escolhe missão, perfil ou executor."""

    model_config = _STRICT

    workflows: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list[str]
    )
    environments: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list[str]
    )
    branches: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=lambda: ["main"]
    )
    repository: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")] | None = None


class AutonomousSettings(BaseModel):
    model_config = _STRICT

    enabled: bool = False
    max_minutes: int = Field(default=DEFAULT_MAX_MINUTES, ge=1, le=600)
    native_max_cost_usd: Usd = DEFAULT_NATIVE_COST
    notifications: list[Channel] = Field(default_factory=list[Channel], max_length=MAX_CHANNELS)
    github: GithubTriggers = Field(default_factory=GithubTriggers)

    @model_validator(mode="after")
    def _unique_names(self) -> Self:
        names = [channel.name for channel in self.notifications]
        if len(names) != len(set(names)):
            raise PydanticCustomError("lastro_channel_unique", "notification names must be unique")
        return self

    @property
    def native_cap_nusd(self) -> int:
        return int(Decimal(self.native_max_cost_usd) * 1_000_000_000)

    def channel(self, name: str) -> SlackChannel | TeamsChannel | SmtpChannel | None:
        return next((item for item in self.notifications if item.name == name), None)

    def snapshot(self) -> dict[str, Any]:
        """Somente nome, tipo e fingerprint redigido dos canais."""
        return {
            "max_minutes": self.max_minutes,
            "native_max_cost_usd": self.native_max_cost_usd,
            "channels": [
                {"name": item.name, "type": item.type, "fingerprint": item.fingerprint()}
                for item in self.notifications
            ],
        }
