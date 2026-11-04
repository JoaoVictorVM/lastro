from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from lastro.config.loader import ProjectContext
from lastro.config.native import NODES, NativeSettings
from lastro.history.runs import config_snapshot


def test_native_defaults() -> None:
    settings = NativeSettings()
    assert settings.model == "claude-sonnet-5-5"
    assert settings.node_models == {}
    assert settings.max_output_tokens == 2048
    assert settings.cost_cap_usd == "2.00"
    assert settings.suite_cost_cap_usd == "30.00"
    assert settings.context_token_limit == 64000
    assert all(settings.model_for(node) == "claude-sonnet-5-5" for node in NODES)


@pytest.mark.parametrize(
    "data",
    [
        {"node_models": {"shell": "claude-sonnet-5-5"}},
        {"node_models": {"observe": "claude-unknown"}},
        {"model": "latest"},
        {"cost_cap_usd": "0"},
        {"cost_cap_usd": "1.1234567"},
        {"cost_cap_usd": "NaN"},
        {"cost_cap_usd": "inf"},
        {"cost_cap_usd": 2.0},
        {"suite_cost_cap_usd": "-1"},
        {"max_output_tokens": 128},
        {"max_output_tokens": 9000},
        {"max_output_tokens": "2048"},
        {"context_token_limit": 1024},
        {"api_key": "sk-ant-not-allowed"},
    ],
)
def test_invalid_node_model_and_decimal_rejected(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        NativeSettings.model_validate(data)


def test_node_override_uses_registry() -> None:
    settings = NativeSettings.model_validate({"node_models": {"observe": "claude-haiku-5-5"}})
    assert settings.model_for("observe") == "claude-haiku-5-5"
    assert settings.model_for("decide") == "claude-sonnet-5-5"
    assert settings.models() == {"claude-sonnet-5-5", "claude-haiku-5-5"}


def test_cli_override_does_not_mutate_project(
    history_context: ProjectContext, workdir: Path
) -> None:
    before = (workdir / "lastro.toml").read_text(encoding="utf-8")
    native = history_context.with_executor("native")
    assert history_context.config.executor == "claude-code"
    assert native.config.executor == "native"
    assert config_snapshot(history_context)["executor"] == "claude-code"
    snapshot = config_snapshot(native)
    assert snapshot["executor"] == "native"
    assert isinstance(snapshot["native"], dict)
    assert len(str(snapshot["native"]["pricing_sha256"])) == 64
    assert "native" not in config_snapshot(history_context)
    assert (workdir / "lastro.toml").read_text(encoding="utf-8") == before


def test_profile_native_table_is_strict(
    write_config: Callable[..., Path], profile_toml: Callable[..., str]
) -> None:
    from lastro.config.errors import ConfigError
    from lastro.config.loader import load_config

    path = write_config(profile_toml("homolog") + "\n[profiles.homolog.native]\ncost_cap_usd = 2\n")
    with pytest.raises(ConfigError):
        load_config(path)
