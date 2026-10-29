import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILES = (".pre-commit-config.yaml", ".gitleaks.toml", ".github/gitleaks-report.tmpl")
REQUIRED_HOOKS = {
    "https://github.com/astral-sh/ruff-pre-commit": {"ruff-check", "ruff-format"},
    "https://github.com/gitleaks/gitleaks": {"gitleaks"},
    "https://github.com/pre-commit/pre-commit-hooks": {
        "end-of-file-fixer",
        "trailing-whitespace",
        "check-added-large-files",
    },
}
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")

GitRunner = Callable[..., subprocess.CompletedProcess[str]]


def load_config() -> dict[str, Any]:
    return yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))


def locked_version(package: str) -> str:
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    return next(item["version"] for item in lock["package"] if item["name"] == package)


def test_required_hooks_are_configured_with_pinned_revisions() -> None:
    repos = {repo["repo"]: repo for repo in load_config()["repos"]}

    for url, hook_ids in REQUIRED_HOOKS.items():
        assert url in repos, url
        assert COMMIT_SHA.match(str(repos[url]["rev"])), f"{url} must pin a commit SHA"
        assert hook_ids <= {hook["id"] for hook in repos[url]["hooks"]}, url
    large_files = next(
        hook
        for hook in repos["https://github.com/pre-commit/pre-commit-hooks"]["hooks"]
        if hook["id"] == "check-added-large-files"
    )
    assert "--maxkb=1024" in large_files["args"]


def test_ruff_hook_matches_locked_ruff_version() -> None:
    text = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    frozen = re.search(r"ruff-pre-commit\n\s+rev: [0-9a-f]{40} # frozen: v(\S+)", text)

    assert frozen is not None
    assert frozen.group(1) == locked_version("ruff")


@pytest.fixture
def hooked_repo(
    tmp_path: Path, make_repo: Callable[[Path], Path], git: GitRunner, host_env: dict[str, str]
) -> Path:
    repo = make_repo(tmp_path / "hooked")
    for name in CONFIG_FILES:
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / name, target)
    installed = subprocess.run(
        [sys.executable, "-m", "pre_commit", "install"],
        cwd=repo,
        env=host_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=120,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert git("add", *CONFIG_FILES, cwd=repo).returncode == 0
    first = commit(git, repo, "chore: initial")
    assert first.returncode == 0, first.stdout + first.stderr
    return repo


def commit(git: GitRunner, repo: Path, message: str) -> subprocess.CompletedProcess[str]:
    # The first commit may install hook environments (gitleaks is built with Go), so allow time.
    return git("commit", "-q", "-m", message, cwd=repo, timeout=900)


def commit_count(git: GitRunner, repo: Path) -> int:
    return int(git("rev-list", "--count", "HEAD", cwd=repo).stdout.strip())


def test_large_file_over_one_megabyte_is_blocked(hooked_repo: Path, git: GitRunner) -> None:
    (hooked_repo / "big.bin").write_bytes(b"\0" * (1025 * 1024))
    assert git("add", "big.bin", cwd=hooked_repo).returncode == 0

    result = commit(git, hooked_repo, "chore: add big file")

    assert result.returncode != 0
    assert "big.bin" in result.stdout + result.stderr
    assert commit_count(git, hooked_repo) == 1


def test_test_aws_key_is_blocked_by_gitleaks(hooked_repo: Path, git: GitRunner) -> None:
    # Assembled at runtime so that no scanner ever sees a key-shaped literal in this checkout.
    fake_key = "AKIA" + "QYLPMN5HHHFPZAM2"
    (hooked_repo / "settings.txt").write_text(
        f"region = sa-east-1\naws_access_key_id = {fake_key}\n", encoding="utf-8"
    )
    assert git("add", "settings.txt", cwd=hooked_repo).returncode == 0

    result = commit(git, hooked_repo, "chore: add settings")

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert (
        "gitleaks: secret detected in settings.txt:2 (aws-access-token). "
        "Remove it or add a reviewed allowlist entry." in output
    ), output
    assert fake_key not in output
    assert commit_count(git, hooked_repo) == 1


def test_example_files_do_not_trigger_secret_scan(hooked_repo: Path, git: GitRunner) -> None:
    examples = (".env.example", "src/lastro_demo/.env.example")
    for name in examples:
        target = hooked_repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / name, target)
    assert git("add", *examples, cwd=hooked_repo).returncode == 0

    result = commit(git, hooked_repo, "docs: add env examples")

    assert result.returncode == 0, result.stdout + result.stderr
    assert commit_count(git, hooked_repo) == 2
