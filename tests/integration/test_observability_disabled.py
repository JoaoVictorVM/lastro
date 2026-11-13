from collections.abc import Callable
from typing import Any

import pytest

from lastro.config.loader import ProjectContext
from lastro.history import RunStatus, TextMission, open_history
from lastro.observability import drain_run, settings_source, start_worker, without_keys
from lastro.observability.worker import ExportWorker
from lastro.reporting.builder import ReportBuilder

from .obs_support import otlp_receiver


def test_disabled_run_mcp_and_report_have_zero_collector_traffic(
    obs_project: Callable[..., ProjectContext],
) -> None:
    with otlp_receiver() as receiver:
        project = obs_project(receiver.base, enabled=False)
        history: Any = open_history(project)
        run = history.create_run(project, TextMission("Explore employees"))
        history.append_event(run.record.id, "oracle_signal", payload={"kind": "console"})

        assert start_worker(project, history, run.record.id) is None
        history.finish_run(run.record.id, RunStatus.COMPLETED)
        drain_run(project, history, run.record.id)
        report = ReportBuilder(history).build_final(run.record.id)

    assert receiver.received == []
    assert history.observability_context(run.record.id) is None
    assert report.data.trace is None


def test_global_otel_environment_cannot_enable_export(
    obs_project: Callable[..., ProjectContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    with otlp_receiver() as receiver:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", receiver.base)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", receiver.base + "/v1/traces")
        monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_on")
        monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
        project = obs_project(receiver.base, enabled=False)
        history: Any = open_history(project)
        run = history.create_run(project, TextMission("Explore employees"))
        history.finish_run(run.record.id, RunStatus.COMPLETED)
        drain_run(project, history, run.record.id)

    assert receiver.received == []


def test_disabling_profile_suspends_new_exports(
    obs_project: Callable[..., ProjectContext],
) -> None:
    with otlp_receiver() as receiver:
        project = obs_project(receiver.base)
        history: Any = open_history(project)
        run = history.create_run(project, TextMission("Explore employees"))
        history.append_event(run.record.id, "oracle_signal", payload={"kind": "console"})
        obs_project(receiver.base, enabled=False)
        ExportWorker(history, run.record.id, settings_source(project)).drain()
        suspended = history.observability_status(run.record.id)

        obs_project(receiver.base)
        ExportWorker(history, run.record.id, settings_source(project)).drain()

    assert (suspended.export_status, suspended.last_error_code) == ("suspended", "obs_suspended")
    assert suspended.pending_spans == 2
    assert len(receiver.spans()) == 1
    assert history.observability_context(run.record.id) is not None


def test_external_child_environment_excludes_langfuse_keys(
    obs_project: Callable[..., ProjectContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    from lastro.codex.preflight import child_environment
    from lastro.reproduction import launchers

    project = obs_project()
    environ = dict(__import__("os").environ)

    scrubbed = without_keys(project.profile.observability, environ)
    assert "LANGFUSE_PUBLIC_KEY" not in scrubbed and "LANGFUSE_SECRET_KEY" not in scrubbed
    assert "PATH" in scrubbed or "Path" in scrubbed
    codex = child_environment(environ)
    assert "LANGFUSE_PUBLIC_KEY" not in codex and "LANGFUSE_SECRET_KEY" not in codex

    captured: dict[str, Any] = {}

    def fake_run(arguments: list[str], **kwargs: Any) -> Any:
        captured.update(kwargs)
        return type("Completed", (), {"returncode": 0})()

    class Created:
        class record:
            id = "run_20261107_120000_a1b2"

    history: Any = open_history(project)
    monkeypatch.setattr(history, "run_dir", lambda run_id: project.project_data_dir)
    monkeypatch.setattr(launchers, "_outcome", lambda *_: None)
    reproducer = launchers.ClaudeReproducer(lambda _: None, run=fake_run)
    prepared = launchers.PreparedChild(
        executor=None,
        skill_bundle_version="x",
        handle="claude",  # type: ignore[arg-type]
    )
    project.project_data_dir.mkdir(parents=True, exist_ok=True)
    reproducer.launch(project, history, Created, prepared)  # type: ignore[arg-type]

    env = captured["env"]
    assert "LANGFUSE_PUBLIC_KEY" not in env and "LANGFUSE_SECRET_KEY" not in env
