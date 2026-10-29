import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

# Captured at collection, before the root conftest isolates HOME: pre-commit and Go reuse their
# caches from the real home directory instead of downloading the hook environments again.
HOST_ENV = dict(os.environ)

GitRunner = Callable[..., subprocess.CompletedProcess[str]]


def _git_env() -> dict[str, str]:
    # The user's global Git config may set hooksPath, signing or excludes that change results.
    return {**HOST_ENV, "GIT_CONFIG_GLOBAL": os.devnull, "PYTHONIOENCODING": "utf-8"}


@pytest.fixture(scope="session")
def host_env() -> dict[str, str]:
    return _git_env()


@pytest.fixture(scope="session")
def git() -> GitRunner:
    executable = shutil.which("git", path=HOST_ENV.get("PATH"))
    if not executable:
        pytest.fail("git is required by the repository contract tests")

    def run(
        *args: str, cwd: Path, stdin: str | None = None, timeout: float = 120
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [executable, *args],
            cwd=cwd,
            env=_git_env(),
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )

    return run


@pytest.fixture(scope="session")
def make_repo(git: GitRunner) -> Callable[[Path], Path]:
    def make(path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        assert git("init", "-q", cwd=path).returncode == 0
        for key, value in (
            ("user.name", "Lastro Test"),
            ("user.email", "lastro-test@example.com"),
            ("commit.gpgsign", "false"),
            ("core.autocrlf", "false"),
        ):
            assert git("config", key, value, cwd=path).returncode == 0
        return path

    return make
