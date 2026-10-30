"""Verificação fail-closed do tenant ativo: api, cookie, jwt e page, nessa ordem.

Erro técnico de um método permite tentar o próximo. Tenant observado e divergente encerra a
cadeia em `mismatch`, sem fallback. Nenhum detalhe devolvido contém cookie, token ou corpo.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, cast
from urllib.parse import urljoin, urlsplit

import jwt
from playwright.async_api import APIResponse, BrowserContext, Page

from lastro.auth.models import (
    TenantVerificationResult,
    VerificationMethod,
    VerificationStatus,
)
from lastro.config.schema import (
    ApiVerification,
    CookieVerification,
    JwtVerification,
    PageVerification,
    Profile,
    Verification,
    is_same_origin,
    origin_of,
)
from lastro.timeutil import utc_now

MAX_JWKS_REDIRECTS = 3
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# Sinais de sessão expirada: o gerenciador renova uma vez antes de decidir.
SESSION_EXPIRED_REASONS = frozenset({"not_authenticated", "login_redirect"})

type Clock = Callable[[], datetime]


class JwksOriginPolicy(Protocol):
    """Ponto de extensão para a política de origens auxiliares do servidor de tools.

    Consultada antes de qualquer resolução DNS ou request a um JWKS fora da origem do target.
    Deve autorizar apenas URL HTTPS cuja origem esteja na allowlist e bloquear destinos SSRF.
    """

    def authorize_jwks_origin(self, url: str) -> bool: ...


@dataclass(frozen=True)
class _Observation:
    tenant: str | None = None
    user: str | None = None
    withheld: bool = False
    reason_code: str = ""
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.tenant is None


def _failure(reason_code: str, detail: str) -> _Observation:
    return _Observation(reason_code=reason_code, detail=detail)


def _json_lookup(data: object, path: str) -> object:
    current = data
    for key in path.split("."):
        if not isinstance(current, dict):
            return None
        current = cast(dict[str, object], current).get(key)
    return current


def _scalar_text(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, int):
        return str(value)
    return None


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _status_failure(response: APIResponse, login_url: str) -> _Observation:
    status = response.status
    if status in (401, 403):
        return _failure("not_authenticated", f"HTTP {status}")
    if status in REDIRECT_STATUSES:
        location = response.headers.get("location", "")
        if location and urlsplit(urljoin(response.url, location)).path == urlsplit(login_url).path:
            return _failure("login_redirect", "redirected to login")
        return _failure("unexpected_redirect", f"HTTP {status}")
    return _failure("http_error", f"HTTP {status}")


class TenantVerifier:
    """Executa a cadeia configurada no perfil contra o contexto de browser corrente."""

    def __init__(
        self,
        profile: Profile,
        *,
        clock: Clock = utc_now,
        jwks_origin_policy: JwksOriginPolicy | None = None,
    ) -> None:
        self.profile = profile
        self._clock = clock
        self._jwks_origin_policy = jwks_origin_policy

    @property
    def _timeout_ms(self) -> float:
        return self.profile.auth.timeout_seconds * 1000

    @property
    def _display_name(self) -> str:
        return self.profile.tenant_display_name or self.profile.tenant_id

    def _result(
        self,
        status: VerificationStatus,
        method: VerificationMethod | None,
        *,
        observed: str | None = None,
        user: str | None = None,
        reason_code: str,
        detail: str,
    ) -> TenantVerificationResult:
        return TenantVerificationResult(
            status=status,
            tenant_expected=self.profile.tenant_id,
            tenant_display_name=self._display_name,
            tenant_observed=observed,
            method=method,
            verified_at=self._clock() if status is VerificationStatus.VERIFIED else None,
            reason_code=reason_code,
            detail=detail,
            observed_user=user if status is VerificationStatus.VERIFIED else None,
        )

    async def verify(
        self, context: BrowserContext, page: Page | None = None
    ) -> TenantVerificationResult:
        methods = self.profile.auth.verification
        if not methods:
            return self._result(
                VerificationStatus.UNVERIFIABLE,
                None,
                reason_code="no_verifier",
                detail="no verification method configured",
            )
        first_failure: tuple[VerificationMethod, _Observation] | None = None
        for item in methods:
            method = VerificationMethod(item.type)
            observation = await self._observe(item, context, page)
            if observation.failed:
                if first_failure is None:
                    first_failure = (method, observation)
                continue
            expected = self._expected_for(item)
            if observation.tenant == expected:
                return self._result(
                    VerificationStatus.VERIFIED,
                    method,
                    observed=self.profile.tenant_id,
                    user=observation.user,
                    reason_code="match",
                    detail="tenant verified",
                )
            return self._result(
                VerificationStatus.MISMATCH,
                method,
                observed=None if observation.withheld else observation.tenant,
                reason_code="tenant_mismatch",
                detail="active tenant differs from authorized tenant",
            )
        assert first_failure is not None
        method, observation = first_failure
        return self._result(
            VerificationStatus.UNVERIFIABLE,
            method,
            reason_code=observation.reason_code,
            detail=observation.detail,
        )

    def _expected_for(self, item: Verification) -> str:
        if isinstance(item, PageVerification):
            return _normalize(item.expected_text or self._display_name)
        return self.profile.tenant_id

    async def _observe(
        self, item: Verification, context: BrowserContext, page: Page | None
    ) -> _Observation:
        try:
            if isinstance(item, ApiVerification):
                return await self._observe_api(item, context)
            if isinstance(item, CookieVerification):
                return await self._observe_cookie(item, context)
            if isinstance(item, JwtVerification):
                return await self._observe_jwt(item, context)
            return await self._observe_page(item, context, page)
        except Exception:
            # Exceções de browser e rede podem citar URLs, headers ou valores; nada é repassado.
            return _failure("verification_error", "verification failed")

    async def _observe_api(self, item: ApiVerification, context: BrowserContext) -> _Observation:
        url = urljoin(self.profile.target_url, item.endpoint)
        if not is_same_origin(url, self.profile.target_url):
            return _failure("origin_denied", "endpoint outside target origin")
        try:
            response = await context.request.get(url, timeout=self._timeout_ms, max_redirects=0)
        except Exception:
            return _failure("request_failed", "request failed")
        try:
            if response.status < 200 or response.status >= 300:
                return _status_failure(response, self.profile.login_url)
            try:
                data: object = await response.json()
            except Exception:
                return _failure("invalid_response", "invalid JSON response")
        finally:
            await response.dispose()
        tenant = _scalar_text(_json_lookup(data, item.tenant_json_path))
        if tenant is None:
            return _failure("tenant_missing", f"{item.tenant_json_path} not found")
        user = None
        if item.user_json_path is not None:
            user = _scalar_text(_json_lookup(data, item.user_json_path))
        return _Observation(tenant=tenant, user=user)

    async def _cookie_value(self, context: BrowserContext, name: str) -> str | None:
        cookies = await context.cookies([self.profile.target_url])
        for cookie in cookies:
            if cookie.get("name") == name:
                value = cookie.get("value")
                return value if value else None
        return None

    async def _observe_cookie(
        self, item: CookieVerification, context: BrowserContext
    ) -> _Observation:
        value = await self._cookie_value(context, item.cookie_name)
        if value is None:
            return _failure("cookie_missing", f"cookie {item.cookie_name} not found")
        # O valor do cookie nunca é devolvido: num mismatch ele fica oculto.
        return _Observation(tenant=value, withheld=True)

    async def _observe_jwt(self, item: JwtVerification, context: BrowserContext) -> _Observation:
        token = await self._cookie_value(context, item.cookie_name)
        if token is None:
            return _failure("token_missing", f"cookie {item.cookie_name} not found")
        jwks_url = urljoin(self.profile.target_url, item.jwks_url)
        denial = self._authorize_jwks(jwks_url)
        if denial is not None:
            return denial
        fetched = await self._fetch_jwks(context, jwks_url)
        if isinstance(fetched, _Observation):
            return fetched
        return _decode_claim(token, fetched, item)

    def _authorize_jwks(self, url: str) -> _Observation | None:
        if is_same_origin(url, self.profile.target_url):
            return None
        if urlsplit(url).scheme != "https":
            return _failure("jwks_origin_denied", "external JWKS requires HTTPS")
        policy = self._jwks_origin_policy
        if policy is None:
            return _failure("jwks_origin_denied", "external JWKS origin not authorized")
        try:
            allowed = policy.authorize_jwks_origin(url)
        except Exception:
            allowed = False
        if allowed is not True:
            return _failure("jwks_origin_denied", "external JWKS origin not authorized")
        return None

    async def _fetch_jwks(
        self, context: BrowserContext, url: str
    ) -> Mapping[str, object] | _Observation:
        approved = origin_of(url)
        current = url
        for _ in range(MAX_JWKS_REDIRECTS + 1):
            response = await context.request.get(current, timeout=self._timeout_ms, max_redirects=0)
            try:
                if response.status in REDIRECT_STATUSES:
                    location = response.headers.get("location", "")
                    following = urljoin(current, location)
                    if not location or origin_of(following) != approved:
                        return _failure("jwks_redirect_denied", "JWKS redirect denied")
                    current = following
                    continue
                if response.status < 200 or response.status >= 300:
                    return _failure("jwks_unavailable", f"JWKS HTTP {response.status}")
                try:
                    data: object = await response.json()
                except Exception:
                    return _failure("jwks_invalid", "invalid JWKS")
            finally:
                await response.dispose()
            if not isinstance(data, dict):
                return _failure("jwks_invalid", "invalid JWKS")
            return cast(dict[str, object], data)
        return _failure("jwks_redirect_denied", "too many JWKS redirects")

    async def _observe_page(
        self, item: PageVerification, context: BrowserContext, page: Page | None
    ) -> _Observation:
        owned = page is None
        target = page if page is not None else await context.new_page()
        try:
            if owned:
                await target.goto(self.profile.target_url, timeout=self._timeout_ms)
            text = await target.locator(item.selector).first.inner_text(timeout=self._timeout_ms)
        except Exception:
            return _failure("element_missing", "tenant element not found")
        finally:
            if owned:
                await target.close()
        normalized = _normalize(text)
        if not normalized:
            return _failure("element_missing", "tenant element is empty")
        return _Observation(tenant=normalized)


_JWT_ERRORS: tuple[tuple[type[jwt.PyJWTError], str, str], ...] = (
    (jwt.ExpiredSignatureError, "token_expired", "token expired"),
    (jwt.ImmatureSignatureError, "token_not_yet_valid", "token not yet valid"),
    (jwt.InvalidAudienceError, "token_audience", "invalid audience"),
    (jwt.InvalidIssuerError, "token_issuer", "invalid issuer"),
    (jwt.MissingRequiredClaimError, "token_claim_missing", "required claim missing"),
    (jwt.InvalidAlgorithmError, "token_algorithm", "algorithm not allowed"),
    (jwt.InvalidSignatureError, "token_signature", "invalid signature"),
)


def _decode_claim(token: str, jwks: Mapping[str, object], item: JwtVerification) -> _Observation:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        return _failure("token_invalid", "malformed token")
    algorithm = header.get("alg")
    if algorithm not in item.algorithms:
        return _failure("token_algorithm", "algorithm not allowed")
    try:
        key_set = jwt.PyJWKSet.from_dict(dict(jwks))
    except jwt.PyJWTError:
        return _failure("jwks_invalid", "invalid JWKS")
    kid = header.get("kid")
    candidates = [key for key in key_set.keys if kid is None or key.key_id == kid]
    if not candidates:
        return _failure("token_key_unknown", "signing key not found")
    claims: dict[str, Any] | None = None
    last_failure = _failure("token_signature", "invalid signature")
    for key in candidates:
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=item.algorithms,
                audience=item.audience,
                issuer=item.issuer,
                options={"require": ["exp", "iss", "aud"]},
            )
            break
        except jwt.PyJWTError as exc:
            last_failure = _jwt_failure(exc)
            if not isinstance(exc, jwt.InvalidSignatureError):
                return last_failure
    if claims is None:
        return last_failure
    tenant = _scalar_text(_json_lookup(claims, item.claim_path))
    if tenant is None:
        return _failure("claim_missing", f"claim {item.claim_path} not found")
    return _Observation(tenant=tenant)


def _jwt_failure(exc: jwt.PyJWTError) -> _Observation:
    for error_type, reason_code, detail in _JWT_ERRORS:
        if isinstance(exc, error_type):
            return _failure(reason_code, detail)
    return _failure("token_invalid", "invalid token")
