from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest

from lastro.autonomous.models import (
    AutonomousError,
    MissionSnapshot,
    QueueStatus,
    TriggerInput,
    TriggerKind,
)
from lastro.autonomous.queue import AutonomousQueue
from lastro.config.loader import ProjectContext
from lastro.history import HistoryStore, RunStatus, TextMission, open_history

MISSION = MissionSnapshot("file", "missions/daily.md", "Explore employees")


def _trigger(key: str, profile: str = "homolog", **overrides: Any) -> TriggerInput:
    data: dict[str, Any] = {
        "kind": TriggerKind.MANUAL,
        "key": key,
        "profile": profile,
        "mission": MISSION,
    }
    data.update(overrides)
    return TriggerInput(**data)


@pytest.fixture
def store(history_context: ProjectContext, fake_clock: Callable[[], datetime]) -> HistoryStore:
    return open_history(history_context, clock=fake_clock)


def _advance(clock_now: list[datetime], seconds: float) -> None:
    clock_now[0] = clock_now[0] + timedelta(seconds=seconds)


def test_duplicate_trigger_is_idempotent(store: HistoryStore, clock_now: list[datetime]) -> None:
    queue = AutonomousQueue(store.autonomous, clock=lambda: clock_now[0])
    first = queue.enqueue(_trigger("manual:1"))
    _advance(clock_now, 1)
    again = queue.enqueue(_trigger("manual:1"))
    assert first.created and not again.created
    assert again.item.id == first.item.id
    assert again.position == first.position == 1
    assert len(store.autonomous.list_items()) == 1


def test_conflicting_trigger_rejected(store: HistoryStore) -> None:
    queue = AutonomousQueue(store.autonomous)
    queue.enqueue(_trigger("manual:1"))
    with pytest.raises(AutonomousError) as conflict:
        queue.enqueue(
            _trigger("manual:1", mission=MissionSnapshot("file", "missions/daily.md", "Other"))
        )
    assert conflict.value.code == "trigger_conflict"


def test_fourth_pending_drops_oldest(store: HistoryStore, clock_now: list[datetime]) -> None:
    queue = AutonomousQueue(store.autonomous, clock=lambda: clock_now[0])
    keeper = queue.acquire("homolog")
    assert keeper is not None
    try:
        queue.enqueue(_trigger("manual:0"))
        active = queue.claim(keeper)
        assert active is not None
        ids: list[str] = []
        for index in range(1, 5):
            _advance(clock_now, 1)
            result = queue.enqueue(_trigger(f"manual:{index}"))
            ids.append(result.item.id)
            if index == 4:
                assert [item.id for item in result.dropped] == [ids[0]]
                assert result.position == 4
    finally:
        keeper.stop()
    items = {item.id: item for item in store.autonomous.list_items()}
    assert items[ids[0]].status == QueueStatus.DROPPED
    assert items[ids[0]].reason == "queue_capacity"
    assert [items[item_id].status for item_id in ids[1:]] == [QueueStatus.QUEUED] * 3
    assert items[active.id].status == QueueStatus.CLAIMED


def test_one_active_per_profile(store: HistoryStore) -> None:
    queue = AutonomousQueue(store.autonomous)
    queue.enqueue(_trigger("manual:1"))
    queue.enqueue(_trigger("manual:2"))
    keeper = queue.acquire("homolog")
    assert keeper is not None
    try:
        assert queue.acquire("homolog") is None
        first = queue.claim(keeper)
        again = queue.claim(keeper)
        assert first is not None and again is not None
        assert again.id == first.id
        busy = queue.enqueue(_trigger("manual:3"))
        assert busy.created and busy.position == 3
    finally:
        keeper.stop()


def test_different_profiles_can_progress(store: HistoryStore) -> None:
    queue = AutonomousQueue(store.autonomous)
    queue.enqueue(_trigger("manual:1"))
    queue.enqueue(_trigger("manual:1", profile="local"))
    homolog = queue.acquire("homolog")
    local = queue.acquire("local")
    assert homolog is not None and local is not None
    try:
        first = queue.claim(homolog)
        second = queue.claim(local)
        assert first is not None and second is not None
        assert {first.profile, second.profile} == {"homolog", "local"}
    finally:
        homolog.stop()
        local.stop()


def test_lease_fencing_and_crash_recovery(store: HistoryStore, clock_now: list[datetime]) -> None:
    clock = lambda: clock_now[0]  # noqa: E731
    alive = {"value": True}
    queue = AutonomousQueue(
        store.autonomous, clock=clock, process_checker=lambda pid: alive["value"]
    )
    queue.enqueue(_trigger("manual:1"))
    crashed = queue.acquire("homolog")
    assert crashed is not None
    crashed.stop(release=False)
    item = queue.claim(crashed)
    assert item is not None
    store.autonomous.mark_running(crashed.lease, item.id)

    # Lease ainda válido: ninguém assume.
    assert queue.acquire("homolog") is None
    _advance(clock_now, 200)
    recovered = queue.acquire("homolog")
    assert recovered is not None
    try:
        assert recovered.lease.generation == crashed.lease.generation + 1
        with pytest.raises(AutonomousError) as lost:
            store.autonomous.finish_item(crashed.lease, item.id, QueueStatus.COMPLETED, None)
        assert lost.value.code == "lease_lost"
        with pytest.raises(AutonomousError):
            crashed.ensure()
        resumed = queue.claim(recovered)
        assert resumed is not None and resumed.id == item.id
        assert resumed.status == QueueStatus.RUNNING
    finally:
        recovered.stop()


def test_existing_run_is_not_duplicated(
    store: HistoryStore, history_context: ProjectContext
) -> None:
    queue = AutonomousQueue(store.autonomous)
    queue.enqueue(_trigger("manual:1"))
    keeper = queue.acquire("homolog")
    assert keeper is not None
    try:
        item = queue.claim(keeper)
        assert item is not None
        run = store.create_run(history_context, TextMission("Explore employees"))
        store.finish_run(run.record.id, RunStatus.COMPLETED, None)
        other = store.create_run(history_context, TextMission("Other"))
        store.finish_run(other.record.id, RunStatus.COMPLETED, None)
        linked = store.autonomous.link_run(keeper.lease, item.id, run.record.id)
        again = store.autonomous.link_run(keeper.lease, item.id, run.record.id)
        assert linked.run_id == again.run_id == run.record.id
        with pytest.raises(AutonomousError):
            store.autonomous.link_run(keeper.lease, item.id, other.record.id)
        assert store.autonomous.item_for_run(run.record.id) is not None
    finally:
        keeper.stop()
