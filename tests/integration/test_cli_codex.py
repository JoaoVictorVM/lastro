from pathlib import Path

import pytest
from typer.testing import CliRunner

from lastro.cli.app import app
from lastro.codex import launcher
from lastro.codex.models import CodexInstall
from lastro.codex.preflight import CodexPreflightError
from lastro.history import RunStatus

from .codex_support import CodexProject, codex_block, make_project


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CodexProject:
    monkeypatch.chdir(tmp_path)
    built = make_project(tmp_path)
    monkeypatch.chdir(built.directory)
    monkeypatch.setattr(launcher, "PROBE", lambda: built.install)
    return built


def _start(runner: CliRunner) -> str:
    result = runner.invoke(app, ["run", "start", "--mission", "Conferir a lista"])
    assert result.exit_code == 0, result.output
    return next(
        line.removeprefix("Run: ") for line in result.output.splitlines() if line.startswith("Run:")
    )


def test_start_prints_supervised_command(runner: CliRunner, project: CodexProject) -> None:
    started = runner.invoke(app, ["run", "start", "--mission", "Conferir a lista"])
    assert started.exit_code == 0, started.output
    run_id = next(
        line.removeprefix("Run: ")
        for line in started.output.splitlines()
        if line.startswith("Run:")
    )
    assert f"lastro run launch --run-id {run_id}" in started.output
    assert "Executor: codex 0.166.0" in started.output

    result_record = project.history().get_run(run_id)
    assert result_record.executor_name == "codex"
    assert result_record.executor_version == "0.166.0"
    assert codex_block(result_record)["launch_pending"] is True
    assert codex_block(result_record)["auth_method"] == "chatgpt"
    assert not (project.bin_dir / "exec-call.json").exists()


def test_login_failure_creates_no_run(
    runner: CliRunner, project: CodexProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse() -> object:
        raise CodexPreflightError("Codex has no ChatGPT login. Run 'codex login'.")

    monkeypatch.setattr(launcher, "PROBE", refuse)

    result = runner.invoke(app, ["run", "start", "--mission", "Conferir"])

    assert result.exit_code == 2
    assert "codex login" in result.output
    assert project.history().get_active_run() is None
    runs_dir = project.context().project_data_dir / "runs"
    assert not runs_dir.exists() or list(runs_dir.iterdir()) == []


def test_missing_rules_refuse_the_start(runner: CliRunner, project: CodexProject) -> None:
    (project.directory / ".codex" / "config.toml").unlink()
    result = runner.invoke(app, ["run", "start", "--mission", "Conferir"])
    assert result.exit_code == 2
    assert "Codex sandbox rules not found; run 'lastro init --executor codex'." in result.output


def test_launch_rechecks_preflight(runner: CliRunner, project: CodexProject) -> None:
    run_id = _start(runner)
    agents = project.directory / "AGENTS.md"
    agents.write_text(agents.read_text(encoding="utf-8") + "\nextra\n", encoding="utf-8")

    result = runner.invoke(app, ["run", "launch", "--run-id", run_id, "--headless"])

    assert result.exit_code == 2
    assert "Codex sandbox rules are invalid" in result.output
    assert codex_block(project.history().get_run(run_id))["launch_pending"] is True


def test_launch_refuses_changed_version(
    runner: CliRunner, project: CodexProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _start(runner)
    changed = CodexInstall(command=project.install.command, version="0.167.0")
    monkeypatch.setattr(launcher, "PROBE", lambda: changed)

    result = runner.invoke(app, ["run", "launch", "--run-id", run_id, "--headless"])

    assert result.exit_code == 2
    assert "Codex changed since run start" in result.output


def test_no_tty_requires_headless(runner: CliRunner, project: CodexProject) -> None:
    run_id = _start(runner)
    result = runner.invoke(app, ["run", "launch", "--run-id", run_id])
    assert result.exit_code == 2
    assert "--headless" in result.output


def test_launch_refuses_unknown_run_and_extra_arguments(
    runner: CliRunner, project: CodexProject
) -> None:
    unknown = runner.invoke(app, ["run", "launch", "--run-id", "run_x", "--headless"])
    assert unknown.exit_code == 2
    assert "not found" in unknown.output
    extra = runner.invoke(
        app, ["run", "launch", "--run-id", "run_x", "--headless", "--", "--search"]
    )
    assert extra.exit_code == 2


def test_headless_prints_normalized_result(runner: CliRunner, project: CodexProject) -> None:
    run_id = _start(runner)

    result = runner.invoke(app, ["run", "launch", "--run-id", run_id, "--headless"])

    assert result.exit_code == 1
    assert '"executor": "codex"' in result.output
    assert '"reason": "executor_exited"' in result.output
    assert project.history().get_run(run_id).status is RunStatus.INTERRUPTED


def test_other_executor_unchanged(runner: CliRunner, project: CodexProject) -> None:
    result = runner.invoke(app, ["doctor"])
    assert "Traceback" not in result.output
    (project.directory / "CLAUDE.md").write_text(
        "skills/lastro/explore.md skills/lastro/qa-heuristics.md skills/lastro/reproduce.md\n",
        encoding="utf-8",
    )
    started = runner.invoke(
        app, ["run", "start", "--executor", "claude-code", "--mission", "Conferir"]
    )
    assert started.exit_code == 0, started.output
    assert "lastro run launch" not in started.output
    active = project.history().get_active_run()
    assert active is not None
    assert active.executor_name == "claude-code"
    assert "codex" not in active.config_snapshot
    refused = runner.invoke(app, ["run", "launch", "--run-id", active.id, "--headless"])
    assert refused.exit_code == 2
    assert "not codex" in refused.output
