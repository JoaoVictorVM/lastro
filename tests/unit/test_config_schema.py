from typing import Any

import pytest
from pydantic import ValidationError

from lastro.config.schema import DEFAULT_TIMEZONE, LastroConfig


def _profile(**overrides: Any) -> dict[str, Any]:
    profile: dict[str, Any] = {
        "target_url": "https://alvo.example.test",
        "login_url": "https://alvo.example.test/login",
        "tenant_id": "acme",
        "allowed_entities": ["employee"],
        "credentials": {"username_env": "HOMOLOG_USERNAME", "password_env": "HOMOLOG_PASSWORD"},
    }
    profile.update(overrides)
    return profile


def _locs(exc: ValidationError) -> list[tuple[int | str, ...]]:
    return [tuple(error["loc"]) for error in exc.errors()]


def test_minimal_profile_valid() -> None:
    config = LastroConfig.model_validate({"profiles": {"homolog": _profile()}})

    profile = config.profiles["homolog"]
    assert profile.name == "homolog"
    assert profile.denied_methods == ["DELETE"]
    assert profile.tenant_display_name == "acme"
    assert profile.editable_fields == {}
    assert profile.version_endpoint is None
    assert config.timezone == DEFAULT_TIMEZONE
    assert config.executor == "claude-code"
    assert config.demo.port == 8765


def test_ten_profiles_ok_eleven_rejected() -> None:
    ten = {f"p{i}": _profile() for i in range(10)}
    assert len(LastroConfig.model_validate({"profiles": ten}).profiles) == 10

    eleven = {f"p{i}": _profile() for i in range(11)}
    with pytest.raises(ValidationError) as excinfo:
        LastroConfig.model_validate({"profiles": eleven})
    assert ("profiles",) in _locs(excinfo.value)


def test_zero_profiles_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        LastroConfig.model_validate({"profiles": {}})
    assert ("profiles",) in _locs(excinfo.value)


def test_invalid_profile_name_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        LastroConfig.model_validate({"profiles": {"Homolog": _profile()}})
    assert "Homolog" in str(excinfo.value)


def test_credentials_hold_names_not_values() -> None:
    profile = _profile(credentials={"username_env": "admin", "password_env": "s3cr3t-value"})
    with pytest.raises(ValidationError) as excinfo:
        LastroConfig.model_validate({"profiles": {"homolog": profile}})

    errors = excinfo.value.errors()
    assert {error["type"] for error in errors} == {"lastro_env_var_name"}
    assert ("profiles", "homolog", "credentials", "password_env") in _locs(excinfo.value)


def test_editable_fields_entity_not_allowed() -> None:
    profile = _profile(editable_fields={"payroll": ["salary"]})
    with pytest.raises(ValidationError) as excinfo:
        LastroConfig.model_validate({"profiles": {"homolog": profile}})

    assert ("profiles", "homolog", "editable_fields") in _locs(excinfo.value)
    assert "payroll" in str(excinfo.value)


def test_unknown_field_rejected() -> None:
    profile = _profile(unexpected_option=True)
    with pytest.raises(ValidationError) as excinfo:
        LastroConfig.model_validate({"profiles": {"homolog": profile}})

    assert ("profiles", "homolog", "unexpected_option") in _locs(excinfo.value)


def test_invalid_timezone_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        LastroConfig.model_validate({"timezone": "Mars/Olympus", "profiles": {"h": _profile()}})
    assert ("timezone",) in _locs(excinfo.value)


@pytest.mark.parametrize("port", [1023, 65536, 0])
def test_demo_port_range(port: int) -> None:
    with pytest.raises(ValidationError) as excinfo:
        LastroConfig.model_validate({"demo": {"port": port}, "profiles": {"h": _profile()}})
    assert ("demo", "port") in _locs(excinfo.value)


@pytest.mark.parametrize("port", [1024, 65535])
def test_demo_port_bounds_accepted(port: int) -> None:
    config = LastroConfig.model_validate({"demo": {"port": port}, "profiles": {"h": _profile()}})
    assert config.demo.port == port


@pytest.mark.parametrize("url", ["alvo.example.test", "ftp://alvo.example.test", "https://"])
def test_target_url_requires_http_scheme_and_host(url: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        LastroConfig.model_validate({"profiles": {"h": _profile(target_url=url)}})
    assert ("profiles", "h", "target_url") in _locs(excinfo.value)


def test_denied_methods_must_be_http_methods() -> None:
    with pytest.raises(ValidationError) as excinfo:
        LastroConfig.model_validate({"profiles": {"h": _profile(denied_methods=["delete"])}})
    assert ("profiles", "h", "denied_methods", 0) in _locs(excinfo.value)


def test_version_endpoint_accepts_path_or_url() -> None:
    for endpoint in ["/api/version", "https://alvo.example.test/version"]:
        config = LastroConfig.model_validate(
            {"profiles": {"h": _profile(version_endpoint=endpoint)}}
        )
        assert config.profiles["h"].version_endpoint == endpoint


def test_run_limits_accept_basic_types_only() -> None:
    limits = {"max_steps": 50, "max_minutes": 1.5, "headless": True, "mode": "safe"}
    config = LastroConfig.model_validate({"profiles": {"h": _profile(run_limits=limits)}})
    assert config.profiles["h"].run_limits == limits

    with pytest.raises(ValidationError) as excinfo:
        LastroConfig.model_validate({"profiles": {"h": _profile(run_limits={"x": [1]})}})
    assert ("profiles", "h", "run_limits") in _locs(excinfo.value)
