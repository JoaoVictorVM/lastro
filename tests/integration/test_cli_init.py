import tomllib
import uuid
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lastro.cli.app import app
from lastro.config.loader import load_config
from lastro.paths import default_private_data_root

FLAGS = [
    "init",
    "--name",
    "homolog",
    "--target-url",
    "https://alvo.example.test",
    "--login-url",
    "https://alvo.example.test/login",
    "--tenant",
    "acme",
]
GENERATED = ["lastro.toml", ".env.example", ".claude/settings.json", "missions/.gitkeep"]
PROMPTS = ["Nome do perfil", "URL do sistema alvo", "URL de login", "Tenant"]


def _assert_generated(workdir: Path) -> None:
    for relative in GENERATED:
        assert (workdir / relative).is_file(), relative
    assert not (workdir / ".env").exists()


def test_init_interactive_four_questions(runner: CliRunner, workdir: Path) -> None:
    answers = "homolog\nhttps://alvo.example.test\nhttps://alvo.example.test/login\nacme\n"

    result = runner.invoke(app, ["init"], input=answers)

    assert result.exit_code == 0, result.output
    for prompt in PROMPTS:
        assert result.stdout.count(f"{prompt}: ") == 1
    lines = result.stdout.splitlines()
    first_result = next(i for i, line in enumerate(lines) if line.startswith("Criado: "))
    assert [line.split(": ")[0] for line in lines[:first_result]] == PROMPTS
    _assert_generated(workdir)


def test_init_interactive_reprompts_invalid_answer(runner: CliRunner, workdir: Path) -> None:
    answers = (
        "Homolog!\nhomolog\nhttps://alvo.example.test\nhttps://alvo.example.test/login\nacme\n"
    )

    result = runner.invoke(app, ["init"], input=answers)

    assert result.exit_code == 0, result.output
    assert result.stdout.count("Nome do perfil: ") == 2
    _assert_generated(workdir)


def test_init_with_flags_non_interactive(runner: CliRunner, workdir: Path) -> None:
    result = runner.invoke(app, FLAGS)

    assert result.exit_code == 0, result.output
    for prompt in PROMPTS:
        assert f"{prompt}: " not in result.stdout
    _assert_generated(workdir)
    assert "lastro doctor" in result.stdout


def test_init_creates_private_root(runner: CliRunner, workdir: Path) -> None:
    root = default_private_data_root()
    assert not root.exists()

    result = runner.invoke(app, FLAGS)

    assert result.exit_code == 0, result.output
    assert root.is_dir()
    assert str(root) in result.stdout


def test_init_warns_when_root_in_git_tree(runner: CliRunner, workdir: Path) -> None:
    home = workdir.parent / "home"
    (home / ".git").mkdir()

    result = runner.invoke(app, FLAGS)

    assert result.exit_code == 0, result.output
    assert "dentro de um repositório Git" in result.stderr


def test_init_invalid_url_rejected(runner: CliRunner, workdir: Path) -> None:
    args = [arg if arg != "https://alvo.example.test" else "alvo.example.test" for arg in FLAGS]

    result = runner.invoke(app, args)

    assert result.exit_code == 2
    assert "http:// ou https://" in result.output
    assert list(workdir.iterdir()) == []
    assert not default_private_data_root().exists()


def test_init_existing_files_exit_1(runner: CliRunner, workdir: Path) -> None:
    assert runner.invoke(app, FLAGS).exit_code == 0
    before = {relative: (workdir / relative).read_bytes() for relative in GENERATED}

    result = runner.invoke(app, [*FLAGS[:2], "outro", *FLAGS[3:]])

    assert result.exit_code == 1
    for relative in GENERATED:
        assert f"Preservado (já existia): {relative}" in result.stdout
        assert (workdir / relative).read_bytes() == before[relative]


LEGACY_CONFIG = """# Configuração anterior, sem identidade do projeto.
# Comentários e ordem devem continuar como estão.

timezone = "America/Recife"
executor = "claude-code"
private_data_root = "../dados-lastro"

[demo]
port = 9000

[profiles.homolog]
target_url = "https://homolog.example.test"
login_url = "https://homolog.example.test/login"
tenant_id = "acme"
tenant_display_name = "ACME Matriz"
allowed_entities = ["employee", "department"]
denied_methods = ["DELETE", "PATCH"]
denied_endpoint_patterns = ["/admin/*"]
run_limits = { max_actions = 40, max_minutes = 30, mode = "strict" }

[profiles.homolog.credentials]
username_env = "HOMOLOG_USERNAME"
password_env = "HOMOLOG_PASSWORD"

[profiles.homolog.editable_fields]
employee = ["phone", "email"]

[profiles.local]
target_url = "http://127.0.0.1:8765"
login_url = "http://127.0.0.1:8765/login"
tenant_id = "aurora"
allowed_entities = ["employee"]

[profiles.local.credentials]
username_env = "LOCAL_USERNAME"
password_env = "LOCAL_PASSWORD"
"""


def test_init_migrates_missing_project_id_and_preserves_all_other_values(
    runner: CliRunner, workdir: Path
) -> None:
    config = workdir / "lastro.toml"
    config.write_text(LEGACY_CONFIG, encoding="utf-8")
    before = tomllib.loads(LEGACY_CONFIG)

    result = runner.invoke(app, FLAGS)

    assert "Migrado: lastro.toml (project_id adicionado)" in result.stdout
    assert "Preservado (já existia): lastro.toml" in result.stdout
    text = config.read_text(encoding="utf-8")
    after = tomllib.loads(text)
    project_id = after.pop("project_id")
    assert str(uuid.UUID(project_id)) == project_id
    assert after == before
    for line in LEGACY_CONFIG.splitlines():
        assert line in text.splitlines()
    loaded = load_config(config)
    assert loaded.project_id == project_id
    assert list(loaded.profiles) == ["homolog", "local"]


def test_init_preserves_existing_project_id(runner: CliRunner, workdir: Path) -> None:
    config = workdir / "lastro.toml"
    config.write_text(LEGACY_CONFIG, encoding="utf-8")
    runner.invoke(app, FLAGS)
    migrated = config.read_bytes()

    result = runner.invoke(app, FLAGS)

    assert "project_id existente preservado em lastro.toml" in result.stdout
    assert "Migrado" not in result.stdout
    assert config.read_bytes() == migrated


def test_init_keeps_windows_line_endings_when_migrating(runner: CliRunner, workdir: Path) -> None:
    config = workdir / "lastro.toml"
    config.write_bytes(LEGACY_CONFIG.replace("\n", "\r\n").encode("utf-8"))

    runner.invoke(app, FLAGS)

    data = config.read_bytes()
    assert b"project_id = " in data
    assert data.count(b"\n") == data.count(b"\r\n")
    assert list(workdir.glob(".lastro.toml.*")) == []


def test_init_does_not_touch_invalid_config(runner: CliRunner, workdir: Path) -> None:
    config = workdir / "lastro.toml"
    config.write_text("profiles = 1\n", encoding="utf-8")

    result = runner.invoke(app, FLAGS)

    assert "project_id não foi adicionado" in result.stderr
    assert config.read_text(encoding="utf-8") == "profiles = 1\n"


def test_new_projects_get_distinct_canonical_project_ids(
    runner: CliRunner, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids: list[str] = []
    for name in ("um", "dois"):
        project = workdir / name
        project.mkdir()
        monkeypatch.chdir(project)
        assert runner.invoke(app, FLAGS).exit_code == 0
        project_id = load_config(project / "lastro.toml").project_id
        assert project_id is not None and str(uuid.UUID(project_id)) == project_id
        ids.append(project_id)

    assert ids[0] != ids[1]
