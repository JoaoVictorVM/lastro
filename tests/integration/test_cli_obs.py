import json
import socket
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from lastro.cli.app import app
from lastro.config.loader import ProjectContext
from lastro.history import RunStatus, TextMission, open_history
from lastro.observability import local

from .obs_support import Reply, closed_port_base, otlp_receiver


class FakeDocker:
    def __init__(self, *, running: bool = False, fail_up: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.running = running
        self.fail_up = fail_up

    def __call__(
        self, arguments: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        args = list(arguments)
        self.calls.append(args)
        if "up" in args:
            self.running = not self.fail_up
            return subprocess.CompletedProcess(args, 1 if self.fail_up else 0, "", "")
        if "ps" in args:
            return subprocess.CompletedProcess(args, 0, "abc\n" if self.running else "", "")
        return subprocess.CompletedProcess(args, 0, "29.0\n", "")


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    fake = FakeDocker()
    monkeypatch.setattr(local, "default_runner", fake)
    monkeypatch.setattr(local, "port_in_use", lambda port: False)
    monkeypatch.setattr(local, "default_probe", lambda url: True)
    return fake


def stack_dir(project: ProjectContext) -> Path:
    return local.stack_for(project.project_data_dir, project.require_project_id()).directory


def test_up_reports_url_after_readiness(
    runner: CliRunner, obs_project: Callable[..., ProjectContext], docker: FakeDocker
) -> None:
    project = obs_project(enabled=False)

    result = runner.invoke(app, ["--profile", "homolog", "obs", "up"])

    assert result.exit_code == 0, result.output
    assert "Langfuse is ready: http://localhost:3000" in result.output
    up = next(call for call in docker.calls if "up" in call)
    assert up[:3] == ["docker", "compose", "--project-name"]
    assert up[3].startswith("lastro-langfuse-")
    assert "Explore" not in " ".join(up)
    compose = (stack_dir(project) / "compose.yaml").read_text(encoding="utf-8")
    assert "127.0.0.1:${LASTRO_LANGFUSE_PORT:?}:3000" in compose
    env_file = (stack_dir(project) / "langfuse.env").read_text(encoding="utf-8")
    values = dict(line.split("=", 1) for line in env_file.splitlines()[1:])
    for name in ("NEXTAUTH_SECRET", "SALT", "ENCRYPTION_KEY", "POSTGRES_PASSWORD", "REDIS_AUTH"):
        assert len(values[name]) == 64
        assert values[name] not in result.output


def test_up_is_idempotent_and_preserves_secrets(
    runner: CliRunner, obs_project: Callable[..., ProjectContext], docker: FakeDocker
) -> None:
    project = obs_project()
    first = runner.invoke(app, ["--profile", "homolog", "obs", "up"])
    secrets_before = (stack_dir(project) / "langfuse.env").read_bytes()
    second = runner.invoke(app, ["--profile", "homolog", "obs", "up"])

    assert first.exit_code == second.exit_code == 0
    assert (stack_dir(project) / "langfuse.env").read_bytes() == secrets_before
    assert not any("down" in call or "-v" in call for call in docker.calls)


def test_up_does_not_enable_profiles(
    runner: CliRunner, obs_project: Callable[..., ProjectContext], docker: FakeDocker
) -> None:
    project = obs_project(enabled=False)
    before = project.config_path.read_bytes()

    assert runner.invoke(app, ["--profile", "homolog", "obs", "up"]).exit_code == 0

    assert project.config_path.read_bytes() == before


def test_up_failure_preserves_volumes(
    runner: CliRunner,
    obs_project: Callable[..., ProjectContext],
    docker: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = obs_project()
    monkeypatch.setattr(local, "default_probe", lambda url: False)
    monkeypatch.setattr(local, "READINESS_SECONDS", 0.0)

    result = runner.invoke(app, ["--profile", "homolog", "obs", "up"])

    assert result.exit_code == 1
    assert "obs_start_timeout" in result.stderr
    assert "Langfuse is ready" not in result.output
    assert (stack_dir(project) / "langfuse.env").exists()
    assert not any("down" in call or "rm" in call for call in docker.calls)

    busy = FakeDocker()
    monkeypatch.setattr(local, "default_runner", busy)
    monkeypatch.setattr(local, "port_in_use", lambda port: True)
    conflict = runner.invoke(app, ["--profile", "homolog", "obs", "up"])
    assert conflict.exit_code == 1
    assert "obs_port_in_use" in conflict.stderr


def test_status_is_offline(
    runner: CliRunner,
    obs_project: Callable[..., ProjectContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = obs_project()
    history = open_history(project)
    run = history.create_run(project, TextMission("Explore employees"))

    def no_network(*_: Any, **__: Any) -> None:
        raise AssertionError("network used")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    result = runner.invoke(app, ["--profile", "homolog", "obs", "status", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["run_id"] == run.record.id
    assert data["export_status"] == "pending"
    assert data["pending_spans"] == 1


def test_export_recovers_without_target_calls(
    runner: CliRunner, obs_project: Callable[..., ProjectContext]
) -> None:
    with otlp_receiver([Reply(status=503)]) as receiver:
        project = obs_project(receiver.base)
        history = open_history(project)
        run = history.create_run(project, TextMission("Explore employees"))
        history.append_event(run.record.id, "oracle_signal", payload={"kind": "console"})
        history.finish_run(run.record.id, RunStatus.COMPLETED)
        command = ["--profile", "homolog", "obs", "export", run.record.id, "--json"]

        failed = runner.invoke(app, command)
        recovered = runner.invoke(app, command)

    assert failed.exit_code == 1
    assert json.loads(failed.output)["last_error_code"] == "export_unavailable"
    assert recovered.exit_code == 0, recovered.output
    data = json.loads(recovered.output)
    assert (data["acknowledged_spans"], data["pending_spans"], data["export_status"]) == (
        3,
        0,
        "idle",
    )
    assert {item.path for item in receiver.received} == {"/api/public/otel/v1/traces"}
    assert receiver.spans()[0].span_id == receiver.received[1].spans()[0].span_id
    assert history.get_run(run.record.id).status == RunStatus.COMPLETED


def test_export_refuses_run_without_opt_in(
    runner: CliRunner, obs_project: Callable[..., ProjectContext]
) -> None:
    project = obs_project(enabled=False)
    history = open_history(project)
    run = history.create_run(project, TextMission("Explore employees"))
    obs_project(closed_port_base())

    result = runner.invoke(app, ["--profile", "homolog", "obs", "export", run.record.id])

    assert result.exit_code == 2
    assert "obs_disabled" in result.stderr


def test_missing_extra_refuses_before_run_creation(
    runner: CliRunner,
    obs_project: Callable[..., ProjectContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lastro.observability as observability

    project = obs_project()
    monkeypatch.setattr(observability, "extra_available", lambda: False)

    result = runner.invoke(
        app, ["--profile", "homolog", "run", "start", "--mission", "Explore employees"]
    )

    assert result.exit_code == 2
    assert "obs_extra_missing" in result.stderr
    assert "uv sync --extra obs" in result.stderr
    assert not project.project_data_dir.exists() or not open_history(project).list_runs()

    monkeypatch.setattr(observability, "extra_available", lambda: True)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY")
    missing_key = runner.invoke(
        app, ["--profile", "homolog", "run", "start", "--mission", "Explore employees"]
    )
    assert missing_key.exit_code == 2
    assert "LANGFUSE_SECRET_KEY" in missing_key.stderr
    assert "obs_credentials_missing" in missing_key.stderr
