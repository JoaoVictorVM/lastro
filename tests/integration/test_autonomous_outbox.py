import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from lastro.autonomous.models import (
    AttemptStatus,
    DeliveryResult,
    DeliveryStatus,
    QueueStatus,
    ScheduleDefinition,
    ScheduleState,
)
from lastro.autonomous.runner import AutonomousPorts
from lastro.cli.app import app
from lastro.config.loader import ProjectContext
from lastro.history import EventType, RunStatus, open_history

from .autonomous_support import (
    FakeExecutor,
    RecordingAdapter,
    coordinator,
    explore,
    found,
    manual,
    with_channels,
)

FAIL = DeliveryResult(AttemptStatus.RETRYABLE, 503, "http_503")
REFUSED = DeliveryResult(AttemptStatus.PERMANENT, 404, "http_404")


def _drain(context: ProjectContext, adapters: dict[str, Any], clock: Any = None) -> Any:
    history = open_history(context)
    history.autonomous.enqueue(manual())
    worker = coordinator(context, history, FakeExecutor(script=explore([found()])))
    extra: dict[str, Any] = {"adapters": adapters, "environ": {}}
    if clock is not None:
        extra["clock"] = clock
    worker.ports = AutonomousPorts(**{**worker.ports.__dict__, "outbox": extra})
    (outcome,) = worker.drain("homolog")
    return history, outcome


def test_channel_failures_are_independent(history_context: ProjectContext) -> None:
    context = with_channels(history_context)
    adapters = {
        "slack": RecordingAdapter(),
        "teams": RecordingAdapter([FAIL]),
        "smtp": RecordingAdapter([REFUSED]),
    }
    history, outcome = _drain(context, adapters)
    records = {r.channel_name: r for r in history.autonomous.list_notifications(outcome.item.id)}
    assert records["qa-slack"].status == DeliveryStatus.DELIVERED
    assert records["qa-teams"].status == DeliveryStatus.PENDING
    assert records["qa-email"].status == DeliveryStatus.FAILED
    assert records["qa-email"].last_error_code == "http_404"
    events = [
        event.payload
        for event in history.list_events(outcome.run.id)
        if event.event_type == EventType.AUTONOMOUS_NOTIFICATION
    ]
    assert sorted(item["channel"] for item in events) == ["qa-email", "qa-slack"]  # type: ignore[index]


def test_three_attempts_end_as_notification_failed(history_context: ProjectContext) -> None:
    context = with_channels(history_context)
    now = [datetime.now(UTC)]
    failing = RecordingAdapter([FAIL, FAIL, FAIL])
    adapters: dict[str, Any] = {
        "slack": failing,
        "teams": RecordingAdapter(),
        "smtp": RecordingAdapter(),
    }
    history, outcome = _drain(context, adapters, clock=lambda: now[0])
    from lastro.autonomous.notifications import Outbox

    outbox = Outbox(history, context.config.autonomous, adapters, environ={}, clock=lambda: now[0])
    for _ in range(3):
        now[0] = now[0] + timedelta(seconds=301)
        history = open_history(context, clock=lambda: now[0])
        outbox.history = history
        outbox.deliver_due()
    (slack,) = [
        r
        for r in history.autonomous.list_notifications(outcome.item.id)
        if r.channel_name == "qa-slack"
    ]
    assert slack.status == DeliveryStatus.FAILED and slack.attempts == 3
    assert len(failing.sent) == 3


def test_tick_drains_due_outbox(
    history_context: ProjectContext, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lastro.cli import schedule_cmd

    context = with_channels(history_context)
    history = open_history(context)
    (workdir / "missions").mkdir()
    (workdir / "missions" / "daily.md").write_text("Explore", encoding="utf-8")
    schedule, _ = history.autonomous.save_schedule(
        ScheduleDefinition(
            id="asch_" + "c" * 32,
            project_id=history.project_id,
            name="daily",
            profile="homolog",
            mission_path="missions/daily.md",
            daily_time="07:00",
            timezone="America/Sao_Paulo",
            executor="claude-code",
            settings={},
            fingerprint="f" * 64,
            backend="fake",
            external_id=None,
            state=ScheduleState.ACTIVE,
            created_at="",
            updated_at="",
            removed_at=None,
        ),
        replace=False,
    )
    item = history.autonomous.enqueue(manual("manual:outbox")).item
    from lastro.autonomous.notifications import build_message

    message = build_message(history, item, None, None, None)
    history.autonomous.create_notifications(item.id, [("qa-slack", "slack")], message)
    monkeypatch.setattr(schedule_cmd, "utc_now", lambda: datetime(2026, 11, 23, 9, 0, tzinfo=UTC))
    monkeypatch.delenv("LASTRO_TEST_SLACK", raising=False)
    from lastro.autonomous import runner

    # O worker não tem trabalho aqui: o tick só precisa drenar a outbox vencida.
    monkeypatch.setattr(runner, "drain_profile", lambda *args, **kwargs: [])
    result = CliRunner().invoke(app, ["schedule", "tick", schedule.id], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    (record,) = history.autonomous.list_notifications(item.id)
    assert record.attempts == 1
    assert record.last_error_code == "webhook_env_missing"


def test_run_status_unchanged_by_delivery_failure(history_context: ProjectContext) -> None:
    context = with_channels(history_context)
    history, outcome = _drain(
        context,
        {
            "slack": RecordingAdapter([REFUSED]),
            "teams": RecordingAdapter([FAIL]),
            "smtp": RecordingAdapter([REFUSED]),
        },
    )
    assert outcome.run.status == RunStatus.COMPLETED
    assert outcome.item.status == QueueStatus.COMPLETED
    assert outcome.report_path is not None and outcome.report_path.is_file()
    statuses = {r.status for r in history.autonomous.list_notifications(outcome.item.id)}
    assert DeliveryStatus.DELIVERED not in statuses
    assert json.loads(json.dumps(outcome.as_json()))["status"] == "completed"
