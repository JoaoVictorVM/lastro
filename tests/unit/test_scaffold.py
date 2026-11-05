import json
from pathlib import Path

import pytest

from lastro.codex.config import SandboxRulesError, load_policy, verify_manifest
from lastro.config.loader import load_config
from lastro.mission import bundle_files, instruction_problems, verify_bundle
from lastro.scaffold.files import (
    CONFIG_FILE,
    ENV_EXAMPLE_FILE,
    MISSION_EXAMPLE,
    MISSIONS_MARKER,
    InitAnswers,
    ProjectIdMigration,
    add_project_id,
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

    assert set(result.created) == {
        CONFIG_FILE,
        ENV_EXAMPLE_FILE,
        PERMISSION_FILE,
        MISSIONS_MARKER,
        MISSION_EXAMPLE,
        Path("CLAUDE.md"),
        Path(".mcp.json"),
        *bundle_files(),
    }
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
    assert settings["permissions"]["allow"] == ["mcp__lastro__*"]
    assert "Bash" in settings["permissions"]["deny"]


def test_existing_files_not_overwritten(tmp_path: Path) -> None:
    (tmp_path / CONFIG_FILE).write_text("# meu arquivo\n", encoding="utf-8")

    result = write_scaffold(tmp_path, ANSWERS)

    assert result.preserved == [CONFIG_FILE]
    assert (tmp_path / CONFIG_FILE).read_text(encoding="utf-8") == "# meu arquivo\n"
    assert ENV_EXAMPLE_FILE in result.created


def test_missions_folder_has_marker_and_example(tmp_path: Path) -> None:
    write_scaffold(tmp_path, ANSWERS)

    missions = tmp_path / "missions"
    assert missions.is_dir()
    assert sorted(child.name for child in missions.iterdir()) == [".gitkeep", "example.md"]
    assert (missions / ".gitkeep").read_text(encoding="utf-8") == ""
    example = (missions / "example.md").read_text(encoding="utf-8")
    for heading in ("# Objetivo", "# Escopo", "# Critérios", "# Fora de escopo", "# Observações"):
        assert heading in example
    assert "Objective, Scope, Criteria, Out of scope, Notes" in example


def test_generated_toml_carries_canonical_project_id(tmp_path: Path) -> None:
    answers = InitAnswers(
        profile_name="homolog",
        target_url="https://alvo.example.test",
        login_url="https://alvo.example.test/login",
        tenant_id="acme",
        project_id="3f0c9a52-6b1e-4d8e-9a77-2a4f1c0d5e61",
    )
    write_scaffold(tmp_path, answers)

    assert load_config(tmp_path / CONFIG_FILE).project_id == answers.project_id
    assert InitAnswers("a", "https://a.test", "https://a.test", "t").project_id != (
        InitAnswers("a", "https://a.test", "https://a.test", "t").project_id
    )


def test_add_project_id_is_idempotent(tmp_path: Path) -> None:
    config = tmp_path / CONFIG_FILE
    config.write_text(
        "[profiles.homolog]\ntarget_url = 'https://a.test'\n", encoding="utf-8", newline="\n"
    )

    assert (
        add_project_id(config, "3f0c9a52-6b1e-4d8e-9a77-2a4f1c0d5e61") is ProjectIdMigration.ADDED
    )
    text = config.read_text(encoding="utf-8")
    assert add_project_id(config) is ProjectIdMigration.PRESERVED
    assert config.read_text(encoding="utf-8") == text
    assert text.index("project_id") < text.index("[profiles.homolog]")


CODEX_FILES = (Path("AGENTS.md"), Path(".codex") / "config.toml", Path(".codex") / "lastro.json")


def test_codex_scaffold(tmp_path: Path) -> None:
    result = write_scaffold(tmp_path, ANSWERS, "codex")

    assert result.diagnostics == []
    for relative in CODEX_FILES:
        assert relative in result.created
    assert not (tmp_path / ".mcp.json").exists()
    assert not (tmp_path / "CLAUDE.md").exists()
    assert load_config(tmp_path / CONFIG_FILE).executor == "codex"
    agents = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert len(agents.splitlines()) <= 60
    assert instruction_problems(tmp_path / "AGENTS.md") == []
    verify_bundle(tmp_path)
    load_policy(tmp_path)
    verify_manifest(tmp_path)


def test_codex_init_is_idempotent(tmp_path: Path) -> None:
    write_scaffold(tmp_path, ANSWERS, "codex")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    again = write_scaffold(tmp_path, ANSWERS, "codex")

    assert again.created == []
    assert again.updated == []
    assert again.diagnostics == []
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


def test_existing_agents_preserved(tmp_path: Path) -> None:
    custom = "# Minhas regras\nNada do bundle aqui.\n"
    (tmp_path / "AGENTS.md").write_text(custom, encoding="utf-8")

    result = write_scaffold(tmp_path, ANSWERS, "codex")

    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == custom
    assert any("não referencia" in item for item in result.diagnostics)
    assert not (tmp_path / ".codex" / "lastro.json").exists()


def test_custom_agents_that_reference_the_bundle_are_accepted(tmp_path: Path) -> None:
    write_scaffold(tmp_path, ANSWERS, "codex")
    agents = tmp_path / "AGENTS.md"
    agents.write_text(agents.read_text(encoding="utf-8") + "\nRegra local.\n", encoding="utf-8")
    with pytest.raises(SandboxRulesError):
        verify_manifest(tmp_path)

    again = write_scaffold(tmp_path, ANSWERS, "codex")

    assert Path(".codex") / "lastro.json" in again.updated
    verify_manifest(tmp_path)


def test_oversized_agents_blocks_the_manifest(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text(
        "skills/lastro/explore.md skills/lastro/qa-heuristics.md skills/lastro/reproduce.md\n"
        + "linha\n" * 80,
        encoding="utf-8",
    )
    result = write_scaffold(tmp_path, ANSWERS, "codex")
    assert any("excede 60 linhas" in item for item in result.diagnostics)
    assert not (tmp_path / ".codex" / "lastro.json").exists()


def test_permissive_existing_codex_config_is_reported(tmp_path: Path) -> None:
    target = tmp_path / ".codex" / "config.toml"
    target.parent.mkdir()
    target.write_text('web_search = "live"\n', encoding="utf-8")

    result = write_scaffold(tmp_path, ANSWERS, "codex")

    assert target.read_text(encoding="utf-8") == 'web_search = "live"\n'
    assert any("Codex sandbox rules are invalid" in item for item in result.diagnostics)


def test_executor_specific_mcp_files(tmp_path: Path) -> None:
    claude = tmp_path / "claude"
    codex = tmp_path / "codex"
    claude.mkdir()
    codex.mkdir()
    write_scaffold(claude, ANSWERS)
    write_scaffold(codex, ANSWERS, "codex")

    assert (claude / ".mcp.json").is_file()
    assert (claude / "CLAUDE.md").is_file()
    assert not (claude / ".codex").exists()
    assert not (codex / ".mcp.json").exists()
    bundle = sorted(path.name for path in (claude / "skills" / "lastro").iterdir())
    assert bundle == sorted(path.name for path in (codex / "skills" / "lastro").iterdir())
    assert verify_bundle(claude).sha256 == verify_bundle(codex).sha256
