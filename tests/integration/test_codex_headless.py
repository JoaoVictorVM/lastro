import json
from pathlib import Path

import pytest

from lastro.codex.launcher import build_arguments
from lastro.codex.models import CodexInstall, LaunchMode, LaunchPlan
from lastro.codex.preflight import CodexPreflightError
from lastro.history import ReportKind

from .codex_support import CodexProject, codex_block, make_project

SAFE_PLAN = {
    "executor": "codex",
    "executor_version": "0.166.0",
    "auth_method": "chatgpt",
    "run_id": "run_1",
    "profile": "local",
    "project_dir": "D:/p q",
    "config_file": "D:/p q/lastro.toml",
    "skill_bundle_sha256": "a" * 64,
}


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CodexProject:
    monkeypatch.chdir(tmp_path)
    return make_project(tmp_path)


def test_exec_arguments() -> None:
    install = CodexInstall(command=("codex",), version="0.166.0")
    plan = LaunchPlan.model_validate({**SAFE_PLAN, "mode": LaunchMode.HEADLESS})

    argv = build_arguments(install, plan, ["--config", "a=1"])

    assert argv[:3] == ["codex", "exec", "--json"]
    assert "--ignore-user-config" in argv
    assert "--ignore-rules" in argv
    assert not any("dangerously" in part or "full-auto" in part for part in argv)
    assert argv[-1].startswith("Run the Lastro mission run_1")


def test_mission_never_reaches_the_arguments(project: CodexProject) -> None:
    run_id = project.create_run()
    project.launch(run_id)
    argv = project.call()["argv"]
    assert not any("Conferir a lista" in part for part in argv)
    assert not any("canary-password" in part for part in argv)


def test_windows_shim_quoting(project: CodexProject) -> None:
    """Caminhos com espaço e aspas chegam intactos, sem passar por um shell."""
    run_id = project.create_run()
    project.launch(run_id)
    call = project.call()
    assert str(project.directory) in call["argv"]
    assert any(part.startswith("developer_instructions=") for part in call["argv"])


def test_zero_exit_is_not_mission_success(project: CodexProject) -> None:
    project.behave(events=[{"type": "turn.completed"}], exit=0)
    run_id = project.create_run()

    result = project.launch(run_id)

    assert (result.status, result.reason, result.exit_code) == (
        "interrupted",
        "executor_exited",
        1,
    )


def test_limit_locks_before_finalize(project: CodexProject) -> None:
    project.behave(
        events=[{"type": "turn.failed", "error": {"codex_error_info": "usage_limit_exceeded"}}],
        hang=True,
    )
    run_id = project.create_run()

    result = project.launch(run_id)

    assert (result.status, result.reason, result.exit_code) == (
        "interrupted",
        "executor_limit",
        3,
    )
    assert result.report_kind == ReportKind.PARTIAL.value
    assert project.history().get_run(run_id).ended_at is not None


def test_partial_report_on_failure(project: CodexProject) -> None:
    project.behave(events=[{"type": "turn.failed", "error": {"message": "boom"}}])
    run_id = project.create_run()

    result = project.launch(run_id)

    assert result.reason == "executor_error"
    assert result.report_kind == "partial"
    assert result.report_path is not None
    assert result.report_path.exists()


def test_invalid_event_stops_with_protocol_error(project: CodexProject) -> None:
    project.behave(raw_lines=["not json\n"], hang=True)
    run_id = project.create_run()

    result = project.launch(run_id)

    assert result.reason == "executor_protocol_error"


def test_oversized_event_stops_with_protocol_error(project: CodexProject) -> None:
    project.behave(raw_lines=['{"type":"x","t":"' + "a" * (1024 * 1024 + 8) + '"}\n'])
    run_id = project.create_run()

    assert project.launch(run_id).reason == "executor_protocol_error"


def test_mcp_startup_failure_is_named(project: CodexProject) -> None:
    project.behave(
        stderr="Error: required MCP servers failed to initialize: lastro\n",
        exit=1,
    )
    run_id = project.create_run()

    result = project.launch(run_id)

    assert result.reason == "mcp_unavailable"
    assert result.status == "interrupted"


def test_child_environment_has_no_secrets(
    project: CodexProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-canary")
    monkeypatch.setenv("LASTRO_LOCAL_PASSWORD", "canary-password")
    run_id = project.create_run()

    project.launch(run_id)

    env = project.call()["env"]
    assert "OPENAI_API_KEY" not in env
    assert "LASTRO_LOCAL_PASSWORD" not in env
    assert "canary-password" not in json.dumps(env)


def test_failed_verification_keeps_the_handoff(project: CodexProject) -> None:
    run_id = project.create_run()
    project.behave(sandbox_rc=3)

    with pytest.raises(CodexPreflightError, match="did not deny"):
        project.launch(run_id)

    record = project.history().get_run(run_id)
    assert codex_block(record)["launch_pending"] is True
    assert not (project.bin_dir / "exec-call.json").exists()


def test_alternative_tool_blocks_launch(project: CodexProject) -> None:
    run_id = project.create_run()
    project.behave(tools=["exec", "exec_command"])

    with pytest.raises(CodexPreflightError, match="outside the Lastro catalog"):
        project.launch(run_id)

    assert not (project.bin_dir / "exec-call.json").exists()


def test_foreign_mcp_is_disabled_for_the_session(project: CodexProject) -> None:
    project.behave(foreign=["node_repl"])
    run_id = project.create_run()

    project.launch(run_id, LaunchMode.INTERACTIVE)

    assert "mcp_servers.node_repl.enabled=false" in project.call("interactive")["argv"]


def test_headless_ignores_user_servers_instead_of_redefining_them(project: CodexProject) -> None:
    project.behave(foreign=["node_repl"])
    run_id = project.create_run()

    project.launch(run_id)

    argv = project.call()["argv"]
    assert "--ignore-user-config" in argv
    assert not any(part.startswith("mcp_servers.node_repl") for part in argv)


def test_foreign_mcp_that_stays_on_blocks_launch(project: CodexProject) -> None:
    project.behave(foreign=["node_repl"], foreign_stuck=True)
    run_id = project.create_run()

    with pytest.raises(CodexPreflightError, match="other than lastro"):
        project.launch(run_id)
