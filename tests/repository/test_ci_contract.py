import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_TEXT = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
WORKFLOW: dict[Any, Any] = yaml.safe_load(WORKFLOW_TEXT)
PINNED_ACTION = re.compile(r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")


def triggers() -> dict[str, Any]:
    # PyYAML follows YAML 1.1, where the bare key `on` is read as the boolean True.
    return WORKFLOW["on"] if "on" in WORKFLOW else WORKFLOW[True]


def job() -> dict[str, Any]:
    (only,) = WORKFLOW["jobs"].values()
    return only


def run_commands() -> list[str]:
    return [step["run"] for step in job()["steps"] if "run" in step]


def test_ci_runs_on_push_and_pull_request() -> None:
    events = triggers()

    assert "push" in events
    assert "pull_request" in events


def test_ci_matrix_contains_windows_and_ubuntu() -> None:
    matrix = job()["strategy"]["matrix"]

    assert set(matrix["os"]) == {"ubuntu-latest", "windows-latest"}
    assert matrix["python-version"] == ["3.12"]
    assert job()["strategy"]["fail-fast"] is False
    assert job()["name"] == "quality (${{ matrix.os }})"


def test_ci_uses_uv_lock_without_resolution_update() -> None:
    commands = run_commands()

    assert "uv sync --locked" in commands
    assert not any("uv lock" in command or "--upgrade" in command for command in commands)
    for step in job()["steps"]:
        if "uses" in step:
            assert PINNED_ACTION.match(step["uses"]), step["uses"]


def test_ci_runs_all_quality_gates() -> None:
    commands = "\n".join(run_commands())

    assert "uv run ruff check ." in commands
    assert "uv run ruff format --check ." in commands
    assert "uv run pyright" in commands
    assert "uv run pytest --cov --cov-report=term --cov-report=xml" in commands
    assert "--cov-fail-under" not in commands
    assert "fail_under" not in (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "gitleaks git" in commands
    assert "sha256sum --check" in commands
    # The ignore contract runs as part of the pytest suite, which collects tests/repository.
    assert (REPO_ROOT / "tests/repository/test_gitignore.py").is_file()
    assert "-k" not in commands.split("uv run pytest", 1)[1].splitlines()[0]


def test_ci_has_no_publish_permission_or_pypi_secret() -> None:
    assert WORKFLOW["permissions"] == {"contents": "read"}
    assert "permissions" not in job()
    assert "environment" not in job()
    lowered = WORKFLOW_TEXT.lower()
    for forbidden in ("secrets.", "pypi", "twine", "uv publish", "id-token", "contents: write"):
        assert forbidden not in lowered, forbidden
    assert job()["steps"][0]["with"]["persist-credentials"] is False
