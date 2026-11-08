from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from lastro.config.errors import ConfigError
from lastro.config.limits import RunLimits
from lastro.config.loader import load_config
from lastro.config.reproduction import (
    ReproductionConfigError,
    ReproductionSettings,
    settings_from_snapshot,
)


def test_defaults() -> None:
    settings = ReproductionSettings()
    assert settings.snapshot() == {
        "attempts": 2,
        "intermittent_attempts": 3,
        "max_minutes": 10,
        "max_actions": 50,
        "mode": "same",
        "verifiers": {},
        "auto": True,
    }
    assert settings.planned_attempts(intermittent=False) == 2
    assert settings.planned_attempts(intermittent=True) == 3
    assert settings.verifier_for("codex") == "codex"


@pytest.mark.parametrize(
    "values",
    [
        {"attempts": 0},
        {"attempts": 6},
        {"intermittent_attempts": 8},
        {"attempts": 3, "intermittent_attempts": 2},
        {"max_minutes": 0},
        {"max_actions": -1},
        {"attempts": True},
        {"attempts": "2"},
        {"mode": "other"},
    ],
)
def test_ranges_and_types(values: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ReproductionSettings.model_validate(values)


def test_cross_requires_pairs() -> None:
    with pytest.raises(ValidationError, match="verifiers"):
        ReproductionSettings.model_validate({"mode": "cross"})


def test_pair_equal_to_explorer_fails() -> None:
    with pytest.raises(ValidationError, match="different from the explorer"):
        ReproductionSettings.model_validate({"mode": "cross", "verifiers": {"codex": "codex"}})


def test_unknown_executor_fails() -> None:
    with pytest.raises(ValidationError, match="unknown: gemini"):
        ReproductionSettings.model_validate(
            {"mode": "cross", "verifiers": {"claude-code": "gemini"}}
        )


def test_unknown_key_fails() -> None:
    with pytest.raises(ValidationError):
        ReproductionSettings.model_validate({"retries": 2})


def test_cross_pair_resolution() -> None:
    settings = ReproductionSettings.model_validate(
        {"mode": "cross", "verifiers": {"claude-code": "codex"}}
    )
    assert settings.verifier_for("claude-code") == "codex"
    with pytest.raises(ReproductionConfigError, match="No verifier configured for explorer native"):
        settings.verifier_for("native")


def test_child_limits_use_the_smaller_value() -> None:
    settings = ReproductionSettings(max_minutes=10, max_actions=50)
    limits = settings.child_limits(RunLimits(max_minutes=5, max_actions=150))
    assert (limits.max_minutes, limits.max_actions) == (5, 50)
    assert limits.max_attempts == RunLimits().max_attempts


def test_snapshot_values_win_and_legacy_uses_current() -> None:
    current = ReproductionSettings(attempts=4, intermittent_attempts=4)
    frozen = settings_from_snapshot({"attempts": 1, "intermittent_attempts": 3}, current)
    assert frozen.attempts == 1
    assert settings_from_snapshot(None, current) == current
    assert settings_from_snapshot({"attempts": "x"}, current) == current


def test_profile_table_loads_and_rejects_unknown_key(
    workdir: Path, write_config: Any, profile_toml: Any
) -> None:
    path = write_config(
        profile_toml(
            "homolog",
            extra="[profiles.homolog.reproduction]\nmode = 'cross'\n"
            "verifiers = { claude-code = 'codex' }\n",
        )
    )
    config = load_config(path, workdir)
    assert config.profiles["homolog"].reproduction.verifiers == {"claude-code": "codex"}

    bad = write_config(
        profile_toml("homolog", extra="[profiles.homolog.reproduction]\nretries = 3\n")
    )
    with pytest.raises(ConfigError):
        load_config(bad, workdir)
