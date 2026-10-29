from pathlib import Path

from typer.testing import CliRunner

from lastro.cli.app import app
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
