"""Autenticação scriptada e verificação de tenant.

API interna consumida pela CLI e pelo servidor de tools. Credenciais, cookies e o conteúdo do
storage state nunca atravessam estes contratos.
"""

from lastro.auth.credentials import LoginCredentials, resolve_credentials
from lastro.auth.models import (
    SESSION_REFRESHED_MESSAGE,
    AuthenticatedSessionRef,
    AuthError,
    AuthEventSubtype,
    AuthTrigger,
    LoginFailedError,
    MissingCredentialError,
    SessionRefError,
    SessionState,
    TenantVerificationResult,
    VerificationMethod,
    VerificationStatus,
    VerifiedTenantContext,
    failure_message,
    login_failed_message,
    mismatch_message,
    missing_credential_message,
    success_message,
    unverifiable_message,
)
from lastro.auth.session import AuthSessionManager, SessionOutcome, run_auth_check
from lastro.auth.storage import MAX_STATE_AGE, SessionStateStore, StorageProtectionError
from lastro.auth.tenant import JwksOriginPolicy, TenantVerifier

__all__ = [
    "MAX_STATE_AGE",
    "SESSION_REFRESHED_MESSAGE",
    "AuthError",
    "AuthEventSubtype",
    "AuthSessionManager",
    "AuthTrigger",
    "AuthenticatedSessionRef",
    "JwksOriginPolicy",
    "LoginCredentials",
    "LoginFailedError",
    "MissingCredentialError",
    "SessionOutcome",
    "SessionRefError",
    "SessionState",
    "SessionStateStore",
    "StorageProtectionError",
    "TenantVerificationResult",
    "TenantVerifier",
    "VerificationMethod",
    "VerificationStatus",
    "VerifiedTenantContext",
    "failure_message",
    "login_failed_message",
    "mismatch_message",
    "missing_credential_message",
    "resolve_credentials",
    "run_auth_check",
    "success_message",
    "unverifiable_message",
]
