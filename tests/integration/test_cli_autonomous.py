import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from lastro.autonomous.queue import AutonomousQueue
from lastro.autonomous.runner import drain_profile
from lastro.cli import autonomous_cmd
from lastro.cli.app import app
from lastro.config.loader import ProjectContext
from lastro.history import open_history

from .autonomous_support import FakeExecutor, explore, found, with_channels


def _invoke(*arguments: str) -> Any:
    return CliRunner().invoke(app, ["--profile", "homolog", *arguments], catch_exceptions=False)


@pytest.fixture
def project(history_context: ProjectContext, workdir: Path) -> ProjectContext:
    (workdir / "missions").mkdir()
    (workdir / "missions" / "daily.md").write_text(
        "Explore employees. Secret detail: internal-only", encoding="utf-8"
    )
    return history_context


def test_manual_trigger_returns_queued_under_busy_lease(project: ProjectContext) -> None:
    history = open_history(project)
    other = AutonomousQueue(history.autonomous).acquire("homolog")
    assert other is not None
    try:
        result = _invoke("autonomous", "run", "--mission-file", "missions/daily.md", "--json")
    finally:
        other.stop()
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["status"] == "queued"
    assert data["worker_started"] is False
    assert data["queue_position"] == 1
    assert data["item_id"].startswith("aq_")
    (item,) = history.autonomous.list_items()
    assert item.mission.text.startswith("Explore employees")
    invalid = _invoke("autonomous", "run", "--json")
    assert invalid.exit_code == 2
    assert json.loads(invalid.stdout)["code"] == "invalid_autonomous_input"


def test_queue_output_is_redacted(project: ProjectContext) -> None:
    history = open_history(project)
    keeper = AutonomousQueue(history.autonomous).acquire("homolog")
    assert keeper is not None
    try:
        _invoke("autonomous", "run", "--mission-file", "missions/daily.md", "--json")
        _invoke("autonomous", "run", "--mission", "Texto inline secreto", "--json")
    finally:
        keeper.stop()
    result = _invoke("autonomous", "queue", "--json")
    assert result.exit_code == 0
    items = json.loads(result.stdout)["items"]
    assert len(items) == 2
    assert "internal-only" not in result.stdout and "inline secreto" not in result.stdout
    assert {item["mission_ref"] for item in items} == {"missions/daily.md", None}


def test_retry_notifications_never_repeats_run(
    project: ProjectContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = with_channels(project)
    fake = FakeExecutor(script=explore([found()]))

    def drain(loaded: Any, store: Any, profile: str) -> Any:
        ports = fake.ports()
        ports.outbox = {"environ": {}}
        return drain_profile(loaded, store, profile, ports=ports)

    monkeypatch.setattr(autonomous_cmd, "drain", drain)
    first = _invoke("autonomous", "run", "--mission-file", "missions/daily.md", "--json")
    assert first.exit_code == 0, first.output
    data = json.loads(first.stdout)
    assert data["status"] == "completed" and data["run_status"] == "completed"
    assert data["report"].endswith("report.autonomous.md")
    history = open_history(context)
    before = len(history.list_runs())
    pending = history.autonomous.list_notifications(data["item_id"])
    assert {item.attempts for item in pending} == {1}
    retried = _invoke("autonomous", "retry-notifications", "--json")
    assert retried.exit_code == 0
    assert json.loads(retried.stdout)["deliveries"] == []
    assert len(history.list_runs()) == before == 1
    assert len(fake.launches) == 1
