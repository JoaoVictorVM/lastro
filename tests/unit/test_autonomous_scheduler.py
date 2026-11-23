from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from lastro.autonomous.models import (
    EnqueueResult,
    OccurrenceStatus,
    ScheduleDefinition,
    ScheduleState,
    TriggerInput,
)
from lastro.autonomous.scheduler import TickOutcome, decide, due_instant, next_due, tick
from lastro.config.loader import ProjectContext
from lastro.history import open_history

NEW_YORK = "America/New_York"


def test_due_in_project_timezone() -> None:
    now = datetime(2026, 11, 23, 10, 5, tzinfo=UTC)
    decision = decide("07:00", "America/Sao_Paulo", now)
    assert decision.status == OccurrenceStatus.QUEUED
    assert decision.local_date == "2026-11-23"
    assert decision.due_at == datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
    early = decide("07:00", "America/Sao_Paulo", datetime(2026, 11, 23, 9, 59, tzinfo=UTC))
    assert early.local_date == "2026-11-22"
    assert early.status == OccurrenceStatus.MISSED
    assert next_due("07:00", "America/Sao_Paulo", now) == datetime(2026, 11, 24, 10, 0, tzinfo=UTC)


def test_ambiguous_time_runs_once() -> None:
    zone = ZoneInfo(NEW_YORK)
    first = due_instant(date(2026, 11, 1), time(1, 30), zone)
    assert first == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    at_first = decide("01:30", NEW_YORK, first + timedelta(minutes=1))
    at_second = decide("01:30", NEW_YORK, first + timedelta(hours=1, minutes=1))
    assert at_first.status == OccurrenceStatus.QUEUED
    assert at_first.local_date == at_second.local_date == "2026-11-01"
    # A segunda passagem por 01:30 é a mesma data local: o tick a trata como duplicata.
    assert at_second.due_at == first


def test_nonexistent_time_uses_first_valid_instant() -> None:
    zone = ZoneInfo(NEW_YORK)
    due = due_instant(date(2026, 3, 8), time(2, 30), zone)
    assert due == datetime(2026, 3, 8, 7, 0, tzinfo=UTC)
    assert due.astimezone(zone).strftime("%H:%M") == "03:00"


def _schedule(mission: str = "missions/daily.md") -> ScheduleDefinition:
    return ScheduleDefinition(
        id="asch_" + "1" * 32,
        project_id="p",
        name="daily",
        profile="homolog",
        mission_path=mission,
        daily_time="07:00",
        timezone="America/Sao_Paulo",
        executor="claude-code",
        settings={},
        fingerprint="f" * 64,
        backend="fake",
        external_id=None,
        state=ScheduleState.ACTIVE,
        created_at="2026-11-01T00:00:00.000000Z",
        updated_at="2026-11-01T00:00:00.000000Z",
        removed_at=None,
    )


@pytest.fixture
def setup(history_context: ProjectContext, workdir: Path) -> tuple[object, ScheduleDefinition]:
    from lastro.autonomous.queue import AutonomousQueue

    (workdir / "missions").mkdir()
    (workdir / "missions" / "daily.md").write_text("Explore employees", encoding="utf-8")
    store = open_history(history_context)
    schedule = _schedule()
    store.autonomous.save_schedule(schedule, replace=False)
    return AutonomousQueue(store.autonomous), schedule


def _tick(setup: tuple[object, ScheduleDefinition], workdir: Path, now: datetime) -> TickOutcome:
    from lastro.autonomous.queue import AutonomousQueue

    queue, schedule = setup
    assert isinstance(queue, AutonomousQueue)
    enqueued: list[TriggerInput] = []

    def enqueue(trigger: TriggerInput) -> EnqueueResult:
        enqueued.append(trigger)
        return queue.enqueue(trigger)

    return tick(queue.repository, schedule, workdir, now, enqueue)


def test_late_tick_recovers_within_60_minutes(
    setup: tuple[object, ScheduleDefinition], workdir: Path
) -> None:
    late = datetime(2026, 11, 23, 10, 59, tzinfo=UTC)
    outcome = _tick(setup, workdir, late)
    again = _tick(setup, workdir, late + timedelta(minutes=1))
    assert outcome.status == OccurrenceStatus.QUEUED
    assert again.status == OccurrenceStatus.DUPLICATE
    assert outcome.enqueued is not None
    item = outcome.enqueued.item
    assert item.trigger_key.endswith(":2026-11-23")
    assert item.mission.text == "Explore employees"


def test_late_tick_after_window_is_missed(
    setup: tuple[object, ScheduleDefinition], workdir: Path
) -> None:
    outcome = _tick(setup, workdir, datetime(2026, 11, 23, 11, 1, tzinfo=UTC))
    assert outcome.status == OccurrenceStatus.MISSED
    occurrence = outcome.occurrence
    assert occurrence is not None
    assert occurrence.reason == "late_beyond_window" and occurrence.item_id is None
    again = _tick(setup, workdir, datetime(2026, 11, 23, 11, 2, tzinfo=UTC))
    assert again.status == OccurrenceStatus.DUPLICATE
