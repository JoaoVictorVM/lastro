import pytest
from pydantic import ValidationError

from lastro.config.limits import RunLimits, limits_from_snapshot
from lastro.config.schema import LastroConfig
from lastro.mission.charter import timebox_limit


def _profile(**extra: object) -> dict[str, object]:
    return {
        "target_url": "https://alvo.example.test",
        "login_url": "https://alvo.example.test/login",
        "tenant_id": "t1",
        "credentials": {"username_env": "U", "password_env": "P"},
        "allowed_entities": [],
        **extra,
    }


def test_defaults_materialized() -> None:
    config = LastroConfig.model_validate({"profiles": {"h": _profile()}})
    assert config.profiles["h"].run_limits.snapshot() == {
        "max_minutes": 30,
        "max_actions": 150,
        "max_attempts": 3,
        "max_no_progress_actions": 10,
    }


@pytest.mark.parametrize(
    "limits",
    [
        {"max_actions": 0},
        {"max_minutes": -1},
        {"max_attempts": True},
        {"max_actions": "150"},
        {"max_minutes": 1.5},
        {"mode": "strict"},
    ],
)
def test_reject_unknown_or_nonpositive_limits(limits: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as excinfo:
        LastroConfig.model_validate({"profiles": {"h": _profile(run_limits=limits)}})
    locations = [tuple(error["loc"]) for error in excinfo.value.errors()]
    assert any(location[:3] == ("profiles", "h", "run_limits") for location in locations)


def test_charter_uses_typed_limits() -> None:
    assert timebox_limit(RunLimits(max_minutes=12)) == 12
    # Snapshot legado normaliza só em memória; valor inválido usa o default da chave.
    assert timebox_limit({"max_minutes": 15}) == 15
    assert timebox_limit({"max_minutes": True}) == 30
    assert limits_from_snapshot({"max_actions": 40}).snapshot() == {
        "max_minutes": 30,
        "max_actions": 40,
        "max_attempts": 3,
        "max_no_progress_actions": 10,
    }
    assert limits_from_snapshot(None) == RunLimits()
