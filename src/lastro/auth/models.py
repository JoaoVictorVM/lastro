"""Contratos de autenticação e tenant: nenhum deles carrega material de sessão."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import NoReturn

SESSION_REFRESHED_MESSAGE = "Session refreshed"
WITHHELD_TENANT = "undisclosed"


class VerificationStatus(StrEnum):
    VERIFIED = "verified"
    MISMATCH = "mismatch"
    UNVERIFIABLE = "unverifiable"


class VerificationMethod(StrEnum):
    API = "api"
    COOKIE = "cookie"
    JWT = "jwt"
    PAGE = "page"


class AuthTrigger(StrEnum):
    SESSION_START = "session_start"
    PRE_WRITE = "pre_write"
    ORIGIN_CHANGE = "origin_change"
    LOGIN_REDIRECT = "login_redirect"
    UNAUTHORIZED = "unauthorized"
    BROWSER_RESTART = "browser_restart"
    DIAGNOSTIC = "diagnostic"


class AuthEventSubtype(StrEnum):
    """Vocabulário fechado de `payload.subtype` dos eventos `authentication`."""

    LOGIN_SUCCEEDED = "login_succeeded"
    LOGIN_FAILED = "login_failed"
    SESSION_REUSED = "session_reused"
    SESSION_REFRESHED = "session_refreshed"
    TENANT_VERIFIED = "tenant_verified"
    TENANT_MISMATCH = "tenant_mismatch"
    TENANT_UNVERIFIABLE = "tenant_unverifiable"


class SessionState(StrEnum):
    """Estado da sessão do perfil; só `VERIFIED` permite avaliar uma escrita."""

    UNAUTHENTICATED = "unauthenticated"
    VERIFIED = "verified"
    MISMATCH = "mismatch"
    UNVERIFIABLE = "unverifiable"
    INVALIDATED = "invalidated"


class _NotSerializable:
    def __reduce__(self) -> NoReturn:
        raise TypeError(f"{type(self).__name__} não pode ser serializado")


@dataclass(frozen=True, eq=False)
class AuthenticatedSessionRef(_NotSerializable):
    """Referência opaca ao storage state publicado; o conteúdo nunca sai do adaptador."""

    profile_name: str
    path: Path
    created_at: datetime
    expires_at: datetime

    def __repr__(self) -> str:
        return (
            f"AuthenticatedSessionRef(profile_name={self.profile_name!r}, "
            f"expires_at={self.expires_at.isoformat()})"
        )

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at


@dataclass(frozen=True)
class VerifiedTenantContext:
    """Só existe depois de um match; o servidor de tools o descarta nos gatilhos."""

    tenant_id: str
    tenant_display_name: str
    method: VerificationMethod
    verified_at: datetime


@dataclass(frozen=True)
class TenantVerificationResult:
    status: VerificationStatus
    tenant_expected: str
    tenant_display_name: str
    tenant_observed: str | None
    method: VerificationMethod | None
    verified_at: datetime | None
    reason_code: str
    detail: str
    observed_user: str | None = None

    @property
    def is_verified(self) -> bool:
        return self.status is VerificationStatus.VERIFIED

    @property
    def allows_writes(self) -> bool:
        return self.is_verified and self.verified_at is not None

    @property
    def method_label(self) -> str:
        return self.method.value if self.method is not None else "none"

    def verified_context(self) -> VerifiedTenantContext | None:
        if not self.allows_writes or self.method is None or self.verified_at is None:
            return None
        return VerifiedTenantContext(
            tenant_id=self.tenant_expected,
            tenant_display_name=self.tenant_display_name,
            method=self.method,
            verified_at=self.verified_at,
        )


class AuthError(Exception):
    """Falha de autenticação com mensagem segura para terminal e eventos."""

    def __init__(self, message: str, reason_code: str) -> None:
        super().__init__(message)
        self.message = message
        self.reason_code = reason_code


class MissingCredentialError(AuthError):
    def __init__(self, variable: str) -> None:
        super().__init__(missing_credential_message(variable), "credential_missing")
        self.variable = variable


class LoginFailedError(AuthError):
    def __init__(self, profile_name: str, reason_code: str) -> None:
        super().__init__(login_failed_message(profile_name), reason_code)
        self.profile_name = profile_name


class SessionRefError(AuthError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(f"Authenticated session reference rejected ({reason_code}).", reason_code)


def missing_credential_message(variable: str) -> str:
    return f"Credential variable {variable} is not set. Add it to .env (see .env.example)."


def login_failed_message(profile_name: str) -> str:
    return (
        f"Login failed for profile '{profile_name}'. "
        "Check credentials and login selectors; no actions were executed."
    )


def mismatch_message(observed: str, expected: str) -> str:
    return (
        f"Active tenant '{observed}' does not match authorized tenant '{expected}'. "
        "Write actions are blocked for this run."
    )


def unverifiable_message(method: str, detail: str) -> str:
    return f"Tenant could not be verified ({method}: {detail}). Write actions are blocked."


def success_message(result: TenantVerificationResult, profile_name: str) -> str:
    verified = f"tenant verified: {result.tenant_display_name} ({result.method_label})"
    if result.observed_user:
        return f"Logged in as {result.observed_user} · {verified}"
    return f"Logged in for profile {profile_name} · {verified}"


def failure_message(result: TenantVerificationResult) -> str:
    if result.status is VerificationStatus.MISMATCH:
        # O elemento da página mostra o nome de exibição, então a comparação é feita com ele.
        expected = (
            result.tenant_display_name
            if result.method is VerificationMethod.PAGE
            else result.tenant_expected
        )
        return mismatch_message(result.tenant_observed or WITHHELD_TENANT, expected)
    return unverifiable_message(result.method_label, result.detail)
