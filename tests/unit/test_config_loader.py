import os
from collections.abc import Callable
from pathlib import Path

import pytest

from lastro.config.errors import ConfigError
from lastro.config.loader import (
    ConfigNotFoundError,
    ProfileSelectionError,
    load_config,
    load_config_file,
    load_project,
    resolve_config_path,
    select_profile,
    select_profiles,
)


def test_discovers_config_in_parent_dir(
    write_config: Callable[..., Path], workdir: Path, profile_toml: Callable[..., str]
) -> None:
    path = write_config(profile_toml())
    nested = workdir / "a" / "b"
    nested.mkdir(parents=True)

    assert resolve_config_path(None, nested) == path


def test_missing_config_reports_init_hint(workdir: Path) -> None:
    with pytest.raises(ConfigNotFoundError) as excinfo:
        resolve_config_path(None, workdir)
    assert "lastro init" in excinfo.value.lines[0]


def test_explicit_config_flag_wins(
    write_config: Callable[..., Path], workdir: Path, profile_toml: Callable[..., str]
) -> None:
    write_config(profile_toml("local"))
    explicit = write_config(profile_toml("outro"), workdir / "elsewhere")

    loaded = load_config_file(Path("elsewhere") / "lastro.toml", workdir)

    assert loaded.path == explicit
    assert list(loaded.config.profiles) == ["outro"]


def test_explicit_config_missing_is_error(workdir: Path) -> None:
    with pytest.raises(ConfigNotFoundError):
        resolve_config_path(Path("nao-existe.toml"), workdir)


def test_env_file_loaded_without_overriding_real_env(
    write_config: Callable[..., Path],
    workdir: Path,
    profile_toml: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(profile_toml())
    (workdir / ".env").write_text(
        "HOMOLOG_USERNAME=from-dotenv\nHOMOLOG_PASSWORD=dotenv-pass\n", encoding="utf-8"
    )
    monkeypatch.setenv("HOMOLOG_USERNAME", "from-real-env")
    monkeypatch.delenv("HOMOLOG_PASSWORD", raising=False)

    load_config_file(None, workdir)

    assert os.environ["HOMOLOG_USERNAME"] == "from-real-env"
    assert os.environ["HOMOLOG_PASSWORD"] == "dotenv-pass"


def test_env_file_read_from_config_dir(
    write_config: Callable[..., Path], workdir: Path, profile_toml: Callable[..., str]
) -> None:
    write_config(profile_toml())
    (workdir / ".env").write_text("FROM_PROJECT_DOTENV=1\n", encoding="utf-8")
    nested = workdir / "sub"
    nested.mkdir()

    load_config_file(None, nested)

    assert os.environ["FROM_PROJECT_DOTENV"] == "1"


def test_profile_selection_single_default(
    write_config: Callable[..., Path], profile_toml: Callable[..., str]
) -> None:
    config = load_config(write_config(profile_toml("homolog")))

    assert select_profile(config, None).name == "homolog"


def test_profile_selection_required_when_many(
    write_config: Callable[..., Path], profile_toml: Callable[..., str]
) -> None:
    config = load_config(write_config(profile_toml("homolog") + profile_toml("local")))

    with pytest.raises(ProfileSelectionError) as excinfo:
        select_profile(config, None)

    message = excinfo.value.lines[0]
    assert "--profile" in message
    assert "homolog" in message and "local" in message
    assert [p.name for p in select_profiles(config, None)] == ["homolog", "local"]
    assert select_profile(config, "local").name == "local"


def test_unknown_profile_error(
    write_config: Callable[..., Path], profile_toml: Callable[..., str]
) -> None:
    config = load_config(write_config(profile_toml("homolog") + profile_toml("local")))

    with pytest.raises(ProfileSelectionError) as excinfo:
        select_profile(config, "producao")

    message = excinfo.value.lines[0]
    assert "producao" in message
    assert "homolog, local" in message


def test_load_project_combines_config_and_profile(
    write_config: Callable[..., Path], workdir: Path, profile_toml: Callable[..., str]
) -> None:
    write_config('private_data_root = "dados"\n' + profile_toml("homolog"))

    project = load_project(cwd=workdir)

    assert project.profile_name == "homolog"
    assert project.private_data_root == workdir / "dados"


def test_invalid_config_raises_config_error(write_config: Callable[..., Path]) -> None:
    with pytest.raises(ConfigError):
        load_config(write_config("profiles = 3\n"))
