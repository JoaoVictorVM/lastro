import json
from pathlib import Path

from lastro.config.loader import load_config
from lastro.scaffold.files import (
    CONFIG_FILE,
    ENV_EXAMPLE_FILE,
    MISSIONS_MARKER,
    InitAnswers,
    credential_variable_names,
    write_scaffold,
)

ANSWERS = InitAnswers(
    profile_name="homolog",
    target_url="https://alvo.example.test",
    login_url="https://alvo.example.test/login",
    tenant_id='acme "matriz"',
)
PERMISSION_FILE = Path(".claude") / "settings.json"


def test_generated_toml_validates(tmp_path: Path) -> None:
    write_scaffold(tmp_path, ANSWERS)

    config = load_config(tmp_path / CONFIG_FILE)

    profile = config.profiles["homolog"]
    assert profile.target_url == ANSWERS.target_url
    assert profile.login_url == ANSWERS.login_url
    assert profile.tenant_id == ANSWERS.tenant_id
    assert profile.tenant_display_name == ANSWERS.tenant_id
    assert profile.denied_methods == ["DELETE"]
    assert profile.credential_variable_names() == list(credential_variable_names("homolog"))


def test_credential_names_are_valid_for_any_profile_name() -> None:
    assert credential_variable_names("homolog") == (
        "LASTRO_HOMOLOG_USERNAME",
        "LASTRO_HOMOLOG_PASSWORD",
    )
    assert credential_variable_names("1st-env")[0] == "LASTRO_1ST_ENV_USERNAME"


def test_no_secret_values_written(tmp_path: Path) -> None:
    result = write_scaffold(tmp_path, ANSWERS)

    assert set(result.created) == {CONFIG_FILE, ENV_EXAMPLE_FILE, PERMISSION_FILE, MISSIONS_MARKER}
    assert not (tmp_path / ".env").exists()
    env_example = (tmp_path / ENV_EXAMPLE_FILE).read_text(encoding="utf-8")
    assignments = [line for line in env_example.splitlines() if line and not line.startswith("#")]
    assert assignments == ["LASTRO_HOMOLOG_USERNAME=", "LASTRO_HOMOLOG_PASSWORD="]
    credentials = load_config(tmp_path / CONFIG_FILE).profiles["homolog"].credentials
    assert credentials.model_dump() == {
        "username_env": "LASTRO_HOMOLOG_USERNAME",
        "password_env": "LASTRO_HOMOLOG_PASSWORD",
    }


def test_permission_file_is_restrictive(tmp_path: Path) -> None:
    write_scaffold(tmp_path, ANSWERS)

    settings = json.loads((tmp_path / PERMISSION_FILE).read_text(encoding="utf-8"))
    assert settings["permissions"]["allow"] == []
    assert "Bash" in settings["permissions"]["deny"]


def test_existing_files_not_overwritten(tmp_path: Path) -> None:
    (tmp_path / CONFIG_FILE).write_text("# meu arquivo\n", encoding="utf-8")

    result = write_scaffold(tmp_path, ANSWERS)

    assert result.preserved == [CONFIG_FILE]
    assert (tmp_path / CONFIG_FILE).read_text(encoding="utf-8") == "# meu arquivo\n"
    assert ENV_EXAMPLE_FILE in result.created


def test_missions_folder_created_empty(tmp_path: Path) -> None:
    write_scaffold(tmp_path, ANSWERS)

    missions = tmp_path / "missions"
    assert missions.is_dir()
    assert [child.name for child in missions.iterdir()] == [".gitkeep"]
    assert (missions / ".gitkeep").read_text(encoding="utf-8") == ""
