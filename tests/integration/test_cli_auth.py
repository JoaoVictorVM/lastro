import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from lastro.cli import auth_cmd
from lastro.cli.app import app
from lastro.history import TextMission, open_history

WITH_USER = (
    '[[profiles.demo.auth.verification]]\ntype = "api"\nendpoint = "/api/me"\n'
    'tenant_json_path = "tenant.id"\nuser_json_path = "user.email"\n'
)


def invoke(runner: CliRunner, project: Any, *args: str) -> Any:
    return runner.invoke(app, ["--config", str(project.config_path), *args])


def _digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def test_auth_check_success_with_observed_identity(
    make_auth_project: Callable[..., Any],
    profile_for: Callable[..., str],
    demo_server: Any,
    runner: CliRunner,
) -> None:
    project = make_auth_project(profile_for("demo", demo_server.url, auth=WITH_USER))

    result = invoke(runner, project, "auth", "check", "--profile", "demo")

    assert result.exit_code == 0, result.output
    assert result.stdout == (
        "Logged in as aurora.teste@example.com · tenant verified: Clínica Aurora (api)\n"
    )


def test_auth_check_success_without_observed_identity_uses_profile(
    make_auth_project: Callable[..., Any], runner: CliRunner
) -> None:
    project = make_auth_project()

    result = runner.invoke(
        app, ["--config", str(project.config_path), "--profile", "demo", "auth", "check"]
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == "Logged in for profile demo · tenant verified: Clínica Aurora (api)\n"


def test_auth_check_does_not_open_or_mutate_history(
    make_auth_project: Callable[..., Any], runner: CliRunner
) -> None:
    project = make_auth_project()
    database = project.project_dir / "history.sqlite"

    first = invoke(runner, project, "auth", "check")
    assert first.exit_code == 0, first.output
    assert not database.exists()
    assert not (project.project_dir / "active-run.json").exists()
    assert sorted(item.name for item in project.project_dir.iterdir()) == [".auth"]

    context = project.context()
    store = open_history(context)
    run_id = store.create_run(context, TextMission("Run alheia")).record.id
    events_before = [event.sequence for event in store.list_events(run_id)]
    snapshot = {
        name: _digest(project.project_dir / name)
        for name in ("history.sqlite", "history.sqlite-wal", "active-run.json")
    }

    second = invoke(runner, project, "auth", "check")

    assert second.exit_code == 0, second.output
    assert [event.sequence for event in store.list_events(run_id)] == events_before
    after = {name: _digest(project.project_dir / name) for name in snapshot}
    assert after == snapshot


def test_missing_credential_exits_before_browser_and_names_variable(
    make_auth_project: Callable[..., Any],
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_auth_project()
    monkeypatch.delenv("DEMO_AURORA_PASSWORD")

    result = invoke(runner, project, "auth", "check")

    assert result.exit_code == 2
    assert result.stderr == (
        "Credential variable DEMO_AURORA_PASSWORD is not set. Add it to .env (see .env.example).\n"
    )
    assert result.stdout == ""
    assert not project.project_dir.exists()


def test_login_failure_and_mismatch_exit_with_messages(
    make_auth_project: Callable[..., Any],
    profile_for: Callable[..., str],
    demo_server: Any,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_auth_project(
        profile_for("demo", demo_server.url),
        profile_for(
            "trocado",
            demo_server.url,
            user_env="DEMO_BOREAL_USER",
            password_env="DEMO_BOREAL_PASSWORD",
        ),
    )

    mismatch = invoke(runner, project, "auth", "check", "--profile", "trocado")
    assert mismatch.exit_code == 2
    assert mismatch.stderr == (
        "Active tenant 'boreal' does not match authorized tenant 'aurora'. "
        "Write actions are blocked for this run.\n"
    )
    assert (project.project_dir / ".auth" / "trocado.json").exists()
    reused = invoke(runner, project, "auth", "check", "--profile", "trocado")
    assert reused.exit_code == 2
    assert reused.stderr == mismatch.stderr

    monkeypatch.setenv("DEMO_AURORA_PASSWORD", "senha-errada-sentinela-cli")
    failed = invoke(runner, project, "auth", "check", "--profile", "demo")
    assert failed.exit_code == 2
    assert failed.stderr == (
        "Login failed for profile 'demo'. "
        "Check credentials and login selectors; no actions were executed.\n"
    )
    assert "senha-errada-sentinela-cli" not in failed.output


def test_auth_check_requires_project_id_before_any_effect(
    make_auth_project: Callable[..., Any], runner: CliRunner
) -> None:
    project = make_auth_project(project_id=None)

    result = invoke(runner, project, "auth", "check")

    assert result.exit_code == 2
    assert "project_id" in result.stderr
    assert not project.private_root.exists()


def test_auth_check_output_never_includes_session_material(
    make_auth_project: Callable[..., Any], runner: CliRunner
) -> None:
    project = make_auth_project()

    result = invoke(runner, project, "auth", "check")

    assert result.exit_code == 0, result.output
    state_path = project.project_dir / ".auth" / "demo.json"
    assert state_path.exists()
    assert str(state_path) not in result.output
    assert ".auth" not in result.output
    assert "lastro_demo_session" not in result.output


def test_browser_unavailable_is_reported(
    make_auth_project: Callable[..., Any],
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from playwright.async_api import Error as PlaywrightError

    project = make_auth_project()

    async def broken(project_context: object) -> Any:
        raise PlaywrightError("Executable doesn't exist")

    monkeypatch.setattr(auth_cmd, "check_runner", broken)
    result = invoke(runner, project, "auth", "check")

    assert result.exit_code == 1
    assert result.stderr == auth_cmd.BROWSER_UNAVAILABLE_MESSAGE + "\n"
