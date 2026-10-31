import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from playwright.async_api import BrowserContext, Page

from lastro.auth import (
    TenantVerificationResult,
    TenantVerifier,
    VerificationMethod,
    VerificationStatus,
    failure_message,
)
from lastro.config.schema import (
    ApiVerification,
    AuthSettings,
    CookieVerification,
    Credentials,
    JwtVerification,
    PageVerification,
    Profile,
    Verification,
)

TARGET = "https://app.example.test"
NOW = datetime(2026, 10, 30, 17, 32, 5, tzinfo=UTC)
ISSUER = "https://app.example.test/idp"
AUDIENCE = "lastro"


@dataclass
class FakeResponse:
    status: int
    body: object = None
    headers: dict[str, str] = field(default_factory=dict[str, str])
    url: str = ""
    disposed: bool = False

    async def json(self) -> object:
        if isinstance(self.body, Exception):
            raise self.body
        return self.body

    async def dispose(self) -> None:
        self.disposed = True


@dataclass
class FakeRequest:
    routes: dict[str, FakeResponse] = field(default_factory=dict[str, FakeResponse])
    calls: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list[tuple[str, dict[str, Any]]]
    )

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        response = self.routes.get(url)
        if response is None:
            raise RuntimeError(f"rota inesperada {url} com cookie=segredo")
        response.url = url
        return response


@dataclass
class FakeLocator:
    text: str | None

    @property
    def first(self) -> "FakeLocator":
        return self

    async def inner_text(self, **kwargs: Any) -> str:
        if self.text is None:
            raise TimeoutError("elemento não encontrado")
        return self.text


@dataclass
class FakePage:
    elements: dict[str, str] = field(default_factory=dict[str, str])
    visited: list[str] = field(default_factory=list[str])
    closed: bool = False

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self.elements.get(selector))

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.visited.append(url)

    async def close(self) -> None:
        self.closed = True


@dataclass
class FakeContext:
    request: FakeRequest = field(default_factory=FakeRequest)
    cookie_jar: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    page: FakePage = field(default_factory=FakePage)
    pages_opened: int = 0

    async def cookies(self, urls: object = None) -> list[dict[str, Any]]:
        return self.cookie_jar

    async def new_page(self) -> FakePage:
        self.pages_opened += 1
        return self.page

    def add_cookie(self, name: str, value: str) -> None:
        self.cookie_jar.append({"name": name, "value": value, "domain": "app.example.test"})


def run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


def make_profile(*verification: Verification, display: str = "Clínica Aurora") -> Profile:
    return Profile(
        name="homolog",
        target_url=TARGET,
        login_url=f"{TARGET}/login",
        tenant_id="aurora",
        tenant_display_name=display,
        credentials=Credentials(username_env="APP_USER", password_env="APP_PASSWORD"),
        allowed_entities=["employee"],
        auth=AuthSettings(verification=list(verification)),
    )


def api(user_path: str | None = "user.email") -> ApiVerification:
    return ApiVerification(
        type="api", endpoint="/api/me", tenant_json_path="tenant.id", user_json_path=user_path
    )


PAGE = PageVerification(type="page", selector="#active-clinic")
COOKIE = CookieVerification(type="cookie", cookie_name="tenant")


def verify(
    profile: Profile, context: FakeContext, policy: object = None
) -> TenantVerificationResult:
    verifier = TenantVerifier(profile, clock=lambda: NOW, jwks_origin_policy=cast(Any, policy))
    return run(verifier.verify(cast(BrowserContext, context)))


def me(tenant: str = "aurora") -> FakeResponse:
    return FakeResponse(
        200,
        {"user": {"email": "aurora.teste@example.com"}, "tenant": {"id": tenant, "name": "X"}},
    )


def test_api_verifier_uses_context_cookie_jar() -> None:
    context = FakeContext(request=FakeRequest({f"{TARGET}/api/me": me()}))

    result = verify(make_profile(api()), context)

    assert result.status is VerificationStatus.VERIFIED
    assert result.method is VerificationMethod.API
    assert result.observed_user == "aurora.teste@example.com"
    assert result.tenant_observed == "aurora"
    assert result.verified_at == NOW
    context_tenant = result.verified_context()
    assert context_tenant is not None
    assert context_tenant.tenant_id == "aurora"
    assert context_tenant.tenant_display_name == "Clínica Aurora"
    url, kwargs = context.request.calls[0]
    assert url == f"{TARGET}/api/me"
    assert "headers" not in kwargs
    assert kwargs["max_redirects"] == 0
    assert context.request.routes[url].disposed


def test_api_without_user_path_has_no_identity() -> None:
    context = FakeContext(request=FakeRequest({f"{TARGET}/api/me": me()}))

    result = verify(make_profile(api(user_path=None)), context)

    assert result.is_verified
    assert result.observed_user is None


def test_api_500_falls_through_or_returns_unverifiable() -> None:
    failing = FakeRequest({f"{TARGET}/api/me": FakeResponse(500, {"error": "boom"})})

    only_api = verify(make_profile(api()), FakeContext(request=failing))

    assert only_api.status is VerificationStatus.UNVERIFIABLE
    assert only_api.method is VerificationMethod.API
    assert only_api.reason_code == "http_error"
    assert only_api.verified_context() is None
    assert not only_api.allows_writes
    assert failure_message(only_api) == (
        "Tenant could not be verified (api: HTTP 500). Write actions are blocked."
    )

    with_page = FakeContext(
        request=FakeRequest({f"{TARGET}/api/me": FakeResponse(500)}),
        page=FakePage({"#active-clinic": "  Clínica\n Aurora "}),
    )
    fallback = verify(make_profile(api(), PAGE), with_page)

    assert fallback.status is VerificationStatus.VERIFIED
    assert fallback.method is VerificationMethod.PAGE
    assert with_page.page.visited == [TARGET]
    assert with_page.page.closed


def test_mismatch_never_falls_back() -> None:
    context = FakeContext(
        request=FakeRequest({f"{TARGET}/api/me": me("boreal")}),
        page=FakePage({"#active-clinic": "Clínica Aurora"}),
    )

    result = verify(make_profile(api(), PAGE), context)

    assert result.status is VerificationStatus.MISMATCH
    assert result.method is VerificationMethod.API
    assert result.tenant_observed == "boreal"
    assert result.observed_user is None
    assert result.verified_context() is None
    assert context.pages_opened == 0
    assert failure_message(result) == (
        "Active tenant 'boreal' does not match authorized tenant 'aurora'. "
        "Write actions are blocked for this run."
    )


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (FakeResponse(401), "not_authenticated"),
        (FakeResponse(403), "not_authenticated"),
        (FakeResponse(303, headers={"location": "/login"}), "login_redirect"),
        (FakeResponse(302, headers={"location": "/elsewhere"}), "unexpected_redirect"),
        (FakeResponse(200, ValueError("json inválido")), "invalid_response"),
        (FakeResponse(200, {"tenant": {}}), "tenant_missing"),
        (FakeResponse(200, {"tenant": {"id": {"nested": 1}}}), "tenant_missing"),
    ],
)
def test_api_technical_failures_are_unverifiable(response: FakeResponse, reason: str) -> None:
    context = FakeContext(request=FakeRequest({f"{TARGET}/api/me": response}))

    result = verify(make_profile(api()), context)

    assert result.status is VerificationStatus.UNVERIFIABLE
    assert result.reason_code == reason


def test_request_exception_detail_is_never_exposed() -> None:
    result = verify(make_profile(api()), FakeContext())

    assert result.reason_code == "request_failed"
    assert "segredo" not in result.detail
    assert "segredo" not in failure_message(result)


def test_cookie_verifier_compares_full_value_and_withholds_it() -> None:
    matching = FakeContext()
    matching.add_cookie("tenant", "aurora")
    assert verify(make_profile(COOKIE), matching).method is VerificationMethod.COOKIE

    other = FakeContext()
    other.add_cookie("tenant", "aurora-cookie-sentinela")
    result = verify(make_profile(COOKIE), other)

    assert result.status is VerificationStatus.MISMATCH
    assert result.tenant_observed is None
    assert "aurora-cookie-sentinela" not in failure_message(result)
    assert "undisclosed" in failure_message(result)

    missing = verify(make_profile(COOKIE), FakeContext())
    assert missing.status is VerificationStatus.UNVERIFIABLE
    assert missing.reason_code == "cookie_missing"


def test_page_verifier_uses_display_name_and_reports_missing_element() -> None:
    mismatch = verify(
        make_profile(PAGE), FakeContext(page=FakePage({"#active-clinic": "Clínica Boreal"}))
    )
    assert mismatch.status is VerificationStatus.MISMATCH
    assert failure_message(mismatch) == (
        "Active tenant 'Clínica Boreal' does not match authorized tenant 'Clínica Aurora'. "
        "Write actions are blocked for this run."
    )

    missing = verify(make_profile(PAGE), FakeContext())
    assert missing.status is VerificationStatus.UNVERIFIABLE
    assert missing.reason_code == "element_missing"

    expected = PageVerification(type="page", selector="#c", expected_text="Aurora")
    assert verify(make_profile(expected), FakeContext(page=FakePage({"#c": "Aurora"}))).is_verified


def test_empty_chain_is_unverifiable() -> None:
    profile = Profile(
        name="leitura",
        target_url=TARGET,
        login_url=f"{TARGET}/login",
        tenant_id="aurora",
        credentials=Credentials(username_env="APP_USER", password_env="APP_PASSWORD"),
        allowed_entities=[],
        auth=AuthSettings(verification=[]),
    )

    result = verify(profile, FakeContext())

    assert result.status is VerificationStatus.UNVERIFIABLE
    assert failure_message(result) == (
        "Tenant could not be verified (none: no verification method configured). "
        "Write actions are blocked."
    )


# JWT --------------------------------------------------------------------------------------


@dataclass
class Keys:
    private: rsa.RSAPrivateKey
    jwks: dict[str, object]


def _keys(kid: str = "k1") -> Keys:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = cast(dict[str, object], RSAAlgorithm.to_jwk(private.public_key(), as_dict=True))
    jwk["kid"] = kid
    return Keys(private=private, jwks={"keys": [jwk]})


@pytest.fixture(scope="module")
def keys() -> Keys:
    return _keys()


def jwt_item(jwks_url: str = "/.well-known/jwks.json") -> JwtVerification:
    return JwtVerification(
        type="jwt",
        cookie_name="id_token",
        claim_path="org.tenant",
        jwks_url=jwks_url,
        issuer=ISSUER,
        audience=AUDIENCE,
        algorithms=["RS256"],
    )


def claims(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": datetime.now(UTC) + timedelta(hours=1),
        "org": {"tenant": "aurora"},
    }
    base.update(overrides)
    return {key: value for key, value in base.items() if value is not None}


def token(keys: Keys, payload: dict[str, object], kid: str = "k1", alg: str = "RS256") -> str:
    return jwt.encode(payload, keys.private, algorithm=alg, headers={"kid": kid})


def jwt_context(value: str, jwks_url: str, jwks: object) -> FakeContext:
    context = FakeContext(request=FakeRequest({jwks_url: FakeResponse(200, jwks)}))
    context.add_cookie("id_token", value)
    return context


def test_jwt_requires_signature_issuer_audience_expiry_and_algorithm(keys: Keys) -> None:
    jwks_url = f"{TARGET}/.well-known/jwks.json"
    profile = make_profile(jwt_item())

    valid = verify(profile, jwt_context(token(keys, claims()), jwks_url, keys.jwks))
    assert valid.status is VerificationStatus.VERIFIED
    assert valid.method is VerificationMethod.JWT

    forged = token(_keys(), claims())
    hs256 = jwt.encode(claims(), "segredo-compartilhado-123456789012", algorithm="HS256")
    unsigned = jwt.encode(claims(), cast(Any, None), algorithm="none")
    cases: list[tuple[str, str]] = [
        (forged, "token_signature"),
        (token(keys, claims(iss="https://evil.example.test")), "token_issuer"),
        (token(keys, claims(aud="outro")), "token_audience"),
        (
            token(keys, claims(exp=datetime.now(UTC) - timedelta(minutes=5))),
            "token_expired",
        ),
        (token(keys, claims(exp=None)), "token_claim_missing"),
        (token(keys, claims(org={"other": "x"})), "claim_missing"),
        (token(keys, claims(), kid="desconhecida"), "token_key_unknown"),
        (hs256, "token_algorithm"),
        (unsigned, "token_algorithm"),
        ("nao-e-um-jwt", "token_invalid"),
    ]
    for value, reason in cases:
        result = verify(profile, jwt_context(value, jwks_url, keys.jwks))
        assert result.status is VerificationStatus.UNVERIFIABLE, reason
        assert result.reason_code == reason
        assert value not in result.detail

    wrong_tenant = verify(
        profile,
        jwt_context(token(keys, claims(org={"tenant": "boreal"})), jwks_url, keys.jwks),
    )
    assert wrong_tenant.status is VerificationStatus.MISMATCH
    assert wrong_tenant.tenant_observed == "boreal"


@dataclass
class PolicyDouble:
    answer: bool | Exception
    asked: list[str] = field(default_factory=list[str])

    def authorize_jwks_origin(self, url: str) -> bool:
        self.asked.append(url)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


EXTERNAL_JWKS = "https://idp.other.test/jwks.json"


def test_external_jwks_fails_closed_without_origin_policy(keys: Keys) -> None:
    context = jwt_context(token(keys, claims()), EXTERNAL_JWKS, keys.jwks)

    result = verify(make_profile(jwt_item(EXTERNAL_JWKS)), context)

    assert result.status is VerificationStatus.UNVERIFIABLE
    assert result.reason_code == "jwks_origin_denied"
    assert context.request.calls == []


def test_jwks_origin_extension_defers_to_tool_server_policy(keys: Keys) -> None:
    profile = make_profile(jwt_item(EXTERNAL_JWKS))
    value = token(keys, claims())

    approved = PolicyDouble(True)
    allowed_context = jwt_context(value, EXTERNAL_JWKS, keys.jwks)
    assert verify(profile, allowed_context, approved).is_verified
    assert approved.asked == [EXTERNAL_JWKS]
    assert [url for url, _ in allowed_context.request.calls] == [EXTERNAL_JWKS]

    for answer in (False, RuntimeError("política indisponível")):
        denied_context = jwt_context(value, EXTERNAL_JWKS, keys.jwks)
        result = verify(profile, denied_context, PolicyDouble(answer))
        assert result.reason_code == "jwks_origin_denied"
        assert denied_context.request.calls == []

    same_origin_policy = PolicyDouble(False)
    same_origin = jwt_context(value, f"{TARGET}/.well-known/jwks.json", keys.jwks)
    assert verify(make_profile(jwt_item()), same_origin, same_origin_policy).is_verified
    assert same_origin_policy.asked == []


def test_jwks_policy_requires_https_and_rechecks_redirect_origin(keys: Keys) -> None:
    value = token(keys, claims())

    plain_http = "http://idp.other.test/jwks.json"
    policy = PolicyDouble(True)
    http_context = jwt_context(value, plain_http, keys.jwks)
    result = verify(make_profile(jwt_item(plain_http)), http_context, policy)
    assert result.reason_code == "jwks_origin_denied"
    assert http_context.request.calls == []

    redirect_out = FakeContext(
        request=FakeRequest(
            {
                EXTERNAL_JWKS: FakeResponse(302, headers={"location": "https://evil.test/jwks"}),
                "https://evil.test/jwks": FakeResponse(200, keys.jwks),
            }
        )
    )
    redirect_out.add_cookie("id_token", value)
    denied = verify(make_profile(jwt_item(EXTERNAL_JWKS)), redirect_out, PolicyDouble(True))
    assert denied.reason_code == "jwks_redirect_denied"
    assert [url for url, _ in redirect_out.request.calls] == [EXTERNAL_JWKS]

    same_origin_url = f"{TARGET}/jwks"
    same_origin_out = FakeContext(
        request=FakeRequest(
            {same_origin_url: FakeResponse(307, headers={"location": "https://evil.test/jwks"})}
        )
    )
    same_origin_out.add_cookie("id_token", value)
    assert (
        verify(make_profile(jwt_item("/jwks")), same_origin_out).reason_code
        == "jwks_redirect_denied"
    )

    redirect_in = FakeContext(
        request=FakeRequest(
            {
                EXTERNAL_JWKS: FakeResponse(301, headers={"location": "/v2/jwks.json"}),
                "https://idp.other.test/v2/jwks.json": FakeResponse(200, keys.jwks),
            }
        )
    )
    redirect_in.add_cookie("id_token", value)
    assert verify(
        make_profile(jwt_item(EXTERNAL_JWKS)), redirect_in, PolicyDouble(True)
    ).is_verified


def test_verifier_never_receives_page_objects_in_result() -> None:
    context = FakeContext(request=FakeRequest({f"{TARGET}/api/me": me()}))
    page = FakePage()
    verifier = TenantVerifier(make_profile(api()), clock=lambda: NOW)

    result = run(verifier.verify(cast(BrowserContext, context), cast(Page, page)))

    assert all(
        not isinstance(value, FakePage | FakeContext | FakeResponse)
        for value in vars(result).values()
    )
