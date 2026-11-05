import subprocess
from pathlib import Path

import pytest

from lastro.codex.models import MINIMUM_VERSION, LaunchMode, LaunchPlan
from lastro.codex.preflight import (
    CodexPreflightError,
    child_environment,
    probe_install,
    resolve_command,
)

HELP = "--no-daemon --config --json --ignore-user-config --ignore-rules --permission-profile"


def runner(
    version: str = "codex-cli 0.166.0",
    login: str = "Logged in using ChatGPT",
    login_rc: int = 0,
    help_text: str = HELP,
    calls: list[list[str]] | None = None,
):
    def run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        if calls is not None:
            calls.append(arguments)
        tail = arguments[1:]
        if tail == ["--version"]:
            return subprocess.CompletedProcess(arguments, 0, version + "\n", "")
        if tail[:2] == ["login", "status"]:
            return subprocess.CompletedProcess(arguments, login_rc, login + "\n", "")
        return subprocess.CompletedProcess(arguments, 0, help_text, "")

    return run


def which(name: str) -> str | None:
    return f"/opt/{name}" if name == "codex" else None


def test_version_floor() -> None:
    assert probe_install(which=which, runner=runner()).version == "0.166.0"
    assert probe_install(which=which, runner=runner("codex-cli 1.2.3")).version == "1.2.3"
    with pytest.raises(CodexPreflightError) as raised:
        probe_install(which=which, runner=runner("codex-cli 0.165.9"))
    assert f"installed: 0.165.9; required: >={MINIMUM_VERSION}" in str(raised.value)


def test_missing_binary() -> None:
    with pytest.raises(CodexPreflightError) as raised:
        probe_install(which=lambda name: None, runner=runner())
    assert f"installed: not found; required: >={MINIMUM_VERSION}" in str(raised.value)


@pytest.mark.parametrize("output", ["codex-cli 0.170.0-alpha.1", "garbage", "codex-cli 1.x"])
def test_invalid_version(output: str) -> None:
    with pytest.raises(CodexPreflightError, match="could not be validated"):
        probe_install(which=which, runner=runner(output))


def test_missing_capability_is_refused() -> None:
    with pytest.raises(CodexPreflightError, match="--no-daemon"):
        probe_install(which=which, runner=runner(help_text="--config"))


@pytest.mark.parametrize(
    ("message", "code"), [("Not logged in", 1), ("Logged in using something new", 0)]
)
def test_chatgpt_login_required(message: str, code: int) -> None:
    with pytest.raises(CodexPreflightError, match="codex login"):
        probe_install(which=which, runner=runner(login=message, login_rc=code))


def test_no_api_fallback() -> None:
    calls: list[list[str]] = []
    with pytest.raises(CodexPreflightError, match="API key"):
        probe_install(which=which, runner=runner(login="Logged in using an API key", calls=calls))
    assert not any(call[1:2] == ["exec"] and "--help" not in call for call in calls)
    assert all(not any("auth.json" in part for part in call) for call in calls)


def test_child_environment_keeps_only_runtime_variables() -> None:
    env = child_environment(
        {
            "PATH": "/bin",
            "OPENAI_API_KEY": "sk-secret",
            "CODEX_API_KEY": "sk-secret",
            "OPENAI_BASE_URL": "https://other.example",
            "LASTRO_LOCAL_PASSWORD": "senha",
            "HTTPS_PROXY": "http://user:pw@proxy",
            "HTTP_PROXY": "http://proxy",
            "LC_ALL": "C",
        }
    )
    assert env == {"PATH": "/bin", "HTTP_PROXY": "http://proxy", "LC_ALL": "C"}


def test_npm_shim_runs_without_cmd(tmp_path: Path) -> None:
    shim = tmp_path / "npm dir" / "codex.cmd"
    script = shim.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    shim.write_text("", encoding="utf-8")
    node = tmp_path / "node.exe"

    command = resolve_command(str(shim), lambda name: str(node))

    assert command == (str(node.resolve()), str(script.resolve()))


def test_unknown_shim_is_refused(tmp_path: Path) -> None:
    shim = tmp_path / "codex.cmd"
    shim.write_text("", encoding="utf-8")
    with pytest.raises(CodexPreflightError, match="not a supported installation"):
        resolve_command(str(shim), lambda name: "node")


def test_packaged_binary_is_used_directly(tmp_path: Path) -> None:
    binary = tmp_path / "codex.exe"
    assert resolve_command(str(binary)) == (str(binary.resolve()),)


@pytest.mark.parametrize(
    "change",
    [
        {"executor_version": "latest"},
        {"skill_bundle_sha256": "ABC"},
        {"run_id": " "},
        {"extra": "x"},
    ],
)
def test_launch_plan_is_strict(change: dict[str, str]) -> None:
    values: dict[str, object] = {
        "executor": "codex",
        "executor_version": "0.166.0",
        "auth_method": "chatgpt",
        "mode": LaunchMode.HEADLESS,
        "run_id": "run_1",
        "profile": "local",
        "project_dir": "D:/p",
        "config_file": "D:/p/lastro.toml",
        "skill_bundle_sha256": "a" * 64,
    }
    LaunchPlan.model_validate(values)
    with pytest.raises(ValueError):
        LaunchPlan.model_validate({**values, **change})
