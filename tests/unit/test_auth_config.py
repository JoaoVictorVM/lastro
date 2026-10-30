from collections.abc import Callable
from pathlib import Path

import pytest

from lastro.config.errors import ConfigError
from lastro.config.loader import load_config
from lastro.config.schema import (
    ApiVerification,
    AuthSettings,
    JwtVerification,
    PageVerification,
)
from lastro.scaffold.files import CONFIG_FILE, InitAnswers, write_scaffold


def _config(
    write_config: Callable[..., Path],
    profile_toml: Callable[..., str],
    auth: str,
    *,
    allowed: str = '["employee"]',
) -> Path:
    block = profile_toml("homolog").replace(
        'allowed_entities = ["employee"]', f"allowed_entities = {allowed}"
    )
    return write_config(f"{block}\n[profiles.homolog.auth]\n{auth}\n")


def _errors(path: Path) -> list[str]:
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    return caught.value.lines


def test_defaults_work_with_demo(
    write_config: Callable[..., Path], profile_toml: Callable[..., str]
) -> None:
    config = load_config(write_config(profile_toml("homolog")))
    auth = config.profiles["homolog"].auth

    assert auth == AuthSettings()
    assert auth.username_selector == '[name="username"]'
    assert auth.password_selector == '[name="password"]'
    assert auth.submit_selector == 'button[type="submit"]'
    assert auth.tenant_selector == '[name="tenant"]'
    assert auth.tenant_submit_selector == 'button[type="submit"]'
    assert auth.timeout_seconds == 15
    assert auth.verification == [
        ApiVerification(type="api", endpoint="/api/me", tenant_json_path="tenant.id")
    ]


def test_write_profile_requires_tenant_verifier(
    write_config: Callable[..., Path], profile_toml: Callable[..., str]
) -> None:
    lines = _errors(_config(write_config, profile_toml, "verification = []"))

    assert any("profiles.homolog.auth" in line for line in lines), lines
    assert any("at least one tenant verification method" in line for line in lines), lines


def test_read_only_profile_may_skip_verifier(
    write_config: Callable[..., Path], profile_toml: Callable[..., str]
) -> None:
    config = load_config(_config(write_config, profile_toml, "verification = []", allowed="[]"))

    assert config.profiles["homolog"].auth.verification == []


API = '{ type = "api", endpoint = "/api/me", tenant_json_path = "tenant.id" }'
COOKIE = '{ type = "cookie", cookie_name = "tenant" }'
PAGE = '{ type = "page", selector = "#active-clinic" }'
JWT = (
    '{ type = "jwt", cookie_name = "id_token", claim_path = "org.id", jwks_url = "/jwks.json", '
    'issuer = "https://idp.example.test", audience = "lastro", algorithms = ["RS256"] }'
)


@pytest.mark.parametrize(
    "items",
    [
        [PAGE, API],
        [COOKIE, API],
        [API, API],
        [API, PAGE, COOKIE],
        [JWT, COOKIE],
    ],
)
def test_verifiers_follow_prd_preference_and_are_unique(
    write_config: Callable[..., Path], profile_toml: Callable[..., str], items: list[str]
) -> None:
    lines = _errors(_config(write_config, profile_toml, f"verification = [{', '.join(items)}]"))

    assert any("in the order api, cookie, jwt, page" in line for line in lines), lines


def test_full_chain_in_preference_order_is_accepted(
    write_config: Callable[..., Path], profile_toml: Callable[..., str]
) -> None:
    config = load_config(
        _config(write_config, profile_toml, f"verification = [{API}, {COOKIE}, {JWT}, {PAGE}]")
    )
    chain = config.profiles["homolog"].auth.verification

    assert [item.type for item in chain] == ["api", "cookie", "jwt", "page"]
    assert isinstance(chain[2], JwtVerification)
    assert chain[2].algorithms == ["RS256"]
    assert isinstance(chain[3], PageVerification)
    assert chain[3].expected_text is None


@pytest.mark.parametrize(
    ("auth", "fragment"),
    [
        ("tenant_submit_selector = false", "expected text"),
        ('tenant_selector = "#t"\ntenant_submit_selector = "nope"\nextra = 1', "unknown field"),
        ("timeout_seconds = 0", "greater than or equal to 1"),
        ("timeout_seconds = 61", "less than or equal to 60"),
        (
            'verification = [{ type = "api", endpoint = "/api/me", tenant_json_path = "a..b" }]',
            "dot-separated keys",
        ),
        (
            'verification = [{ type = "api", endpoint = "https://other.example.test/me", '
            'tenant_json_path = "tenant.id" }]',
            "target_url origin",
        ),
        (JWT.replace('["RS256"]', '["HS256"]').join(["verification = [", "]"]), "asymmetric"),
        ('verification = [{ type = "ldap" }]', "profiles.homolog.auth.verification.0"),
    ],
)
def test_invalid_auth_blocks_are_rejected(
    write_config: Callable[..., Path],
    profile_toml: Callable[..., str],
    auth: str,
    fragment: str,
) -> None:
    lines = _errors(_config(write_config, profile_toml, auth))

    assert any(fragment in line for line in lines), lines


def test_tenant_selector_requires_submit_selector(
    write_config: Callable[..., Path], profile_toml: Callable[..., str]
) -> None:
    with pytest.raises(ValueError, match="tenant_submit_selector"):
        AuthSettings.model_validate({"tenant_selector": "#t", "tenant_submit_selector": None})
    lines = _errors(
        _config(write_config, profile_toml, 'tenant_selector = "#t"\ntenant_submit_selector = 1')
    )
    assert any("profiles.homolog.auth.tenant_submit_selector" in line for line in lines)


def test_api_endpoint_on_target_origin_is_accepted(
    write_config: Callable[..., Path], profile_toml: Callable[..., str]
) -> None:
    auth = (
        'verification = [{ type = "api", endpoint = "https://homolog.example.test:443/api/me", '
        'tenant_json_path = "tenant.id", user_json_path = "user.email" }]'
    )
    config = load_config(_config(write_config, profile_toml, auth))
    item = config.profiles["homolog"].auth.verification[0]

    assert isinstance(item, ApiVerification)
    assert item.user_json_path == "user.email"


def test_scaffold_template_carries_demo_compatible_auth_defaults(tmp_path: Path) -> None:
    answers = InitAnswers(
        profile_name="homolog",
        target_url="https://alvo.example.test",
        login_url="https://alvo.example.test/login",
        tenant_id="aurora",
    )
    write_scaffold(tmp_path, answers)
    text = (tmp_path / CONFIG_FILE).read_text(encoding="utf-8")

    assert "[profiles.homolog.auth]" in text
    assert load_config(tmp_path / CONFIG_FILE).profiles["homolog"].auth == AuthSettings()
