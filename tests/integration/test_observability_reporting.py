import json
import socket
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from lastro.config.loader import ProjectContext
from lastro.history import RunStatus, TextMission, open_history
from lastro.reporting.builder import ReportBuilder, canonical, data_sha256, sha256_text
from lastro.reporting.public import export_public, strip_trace

BASE = "http://127.0.0.1:3000"


def text(path: Path | None) -> str:
    assert path is not None
    return path.read_text(encoding="utf-8")


def finished(project: ProjectContext) -> tuple[Any, str]:
    history = open_history(project)
    run = history.create_run(project, TextMission("Explore employees"))
    history.finish_run(run.record.id, RunStatus.COMPLETED)
    return history, run.record.id


def expected_link(history: Any, run_id: str) -> str:
    trace_id = history.observability_context(run_id).trace_id
    return f"{BASE}/project/proj-lastro-test/traces/{trace_id}"


def test_private_report_contains_frozen_trace_link(
    obs_project: Callable[..., ProjectContext],
) -> None:
    history, run_id = finished(obs_project(BASE))
    link = expected_link(history, run_id)
    obs_project("https://cloud.langfuse.com")

    outcome = ReportBuilder(history).build_final(run_id)

    assert outcome.data.trace is not None
    assert outcome.data.trace.trace_url == link
    assert outcome.data.trace.label == "Langfuse"
    report = text(outcome.report_path)
    assert "## Trace" in report
    assert link in report
    assert "acknowledged" not in report.split("## Trace")[1].split("##")[0]


def test_partial_and_fallback_include_same_link(
    obs_project: Callable[..., ProjectContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = obs_project(BASE)
    history, run_id = finished(project)
    link = expected_link(history, run_id)
    partial = ReportBuilder(history).build_partial(run_id, "limit_reached")
    assert link in text(partial.report_path)

    def broken(self: Any, data: Any) -> str:
        raise RuntimeError("template failed")

    other_history, other_run = finished(project)
    monkeypatch.setattr(ReportBuilder, "render_report", broken)
    fallback = ReportBuilder(other_history).build_partial(other_run, "cancelled")

    raw = json.loads(text(fallback.fallback_path))
    assert expected_link(other_history, other_run) in json.dumps(raw)


def test_public_projection_removes_trace_view(
    obs_project: Callable[..., ProjectContext],
) -> None:
    history, run_id = finished(obs_project(BASE))
    private = ReportBuilder(history).build_final(run_id)
    trace_id = history.observability_context(run_id).trace_id

    exported = export_public(history, run_id)

    public_text = exported.path.read_text(encoding="utf-8")
    assert trace_id not in public_text
    assert "## Trace" not in public_text
    private_text = text(private.report_path)
    stripped = strip_trace(private_text)
    assert trace_id not in stripped
    assert "## Mission and charter" in stripped


def test_disabled_and_legacy_report_digests_are_unchanged(
    obs_project: Callable[..., ProjectContext],
) -> None:
    history, run_id = finished(obs_project(enabled=False))
    outcome = ReportBuilder(history).build_final(run_id)

    content = asdict(outcome.data)
    assert content.pop("trace") is None
    for key in ("model_usage", "change_context", "execution"):
        if content.get(key) is None:
            content.pop(key, None)
    for group in ("confirmed", "suspected", "harness_failures"):
        assert not content[group]
    assert data_sha256(outcome.data) == sha256_text(canonical(content))
    assert "## Trace" not in text(outcome.report_path)


def test_report_build_makes_no_network_calls(
    obs_project: Callable[..., ProjectContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    history, run_id = finished(obs_project(BASE))

    def no_network(*_: Any, **__: Any) -> None:
        raise AssertionError("network used")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    outcome = ReportBuilder(history).build_final(run_id)

    assert outcome.data.trace is not None


def test_report_build_command_prints_private_link(
    obs_project: Callable[..., ProjectContext],
) -> None:
    from typer.testing import CliRunner

    from lastro.cli.app import app

    history, run_id = finished(obs_project(BASE))
    result = CliRunner().invoke(app, ["--profile", "homolog", "report", "build", run_id])

    assert result.exit_code == 0, result.output
    assert expected_link(history, run_id) in result.output
    again = CliRunner().invoke(app, ["--profile", "homolog", "report", "build", run_id])
    assert again.exit_code == 0
    assert again.output == result.output
