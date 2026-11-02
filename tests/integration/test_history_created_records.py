import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta

import pytest

from lastro.browser.events import record_call
from lastro.browser.policy import Decision
from lastro.browser.provenance import Provenance
from lastro.config.loader import ProjectContext
from lastro.history import (
    CleanupStatus,
    CleanupTransitionError,
    CreatedRecordInput,
    CreatedRecordNotFoundError,
    EventType,
    HistoryStore,
    HistoryUnavailableError,
    InvalidEventError,
    TextMission,
    open_history,
    unidentified_warning,
)
from lastro.history import created_records as sql
from lastro.history.database import Database


def authorized(store: HistoryStore, run_id: str, action_id: str = "create-1") -> str:
    record_call(store, run_id, action_id, "submit", {}, Decision("allow", "rule", ""))
    return action_id


def creation(run_id: str, action_id: str = "create-1", **changes: object) -> CreatedRecordInput:
    data: dict[str, object] = {
        "run_id": run_id,
        "action_id": action_id,
        "entity": "employee",
        "system_identifier": "42",
        "tenant": "acme",
        "expected_tenant": "acme",
        "detail_path": "/employees/42",
        "cleanup_instructions": "Excluir em Configurações.",
        **changes,
    }
    return CreatedRecordInput(**data)  # pyright: ignore[reportArgumentType]


def count(store: HistoryStore, table: str) -> int:
    conn = sqlite3.connect(store.database_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_record_and_event_commit_atomically(
    history_context: ProjectContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = open_history(history_context)
    run_id = store.create_run(history_context, TextMission("create")).record.id
    authorized(store, run_id)

    record = store.record_created(creation(run_id))

    assert record.id.startswith("created_")
    assert (record.entity_type, record.system_identifier, record.tenant) == (
        "employee",
        "42",
        "acme",
    )
    assert record.cleanup_status == CleanupStatus.PENDING
    assert record.identified and record.warnings == ()
    assert record.detail_path == "/employees/42"
    assert record.profile == "homolog"
    (event,) = [e for e in store.list_events(run_id) if e.event_type == EventType.RECORD_CREATED]
    assert isinstance(event.payload, dict)
    assert event.payload["tracking_id"] == record.id
    assert event.payload["record_id"] == "42"
    assert event.payload["cleanup_status"] == "pending"
    payload = json.loads(
        sqlite3.connect(store.database_path)
        .execute("SELECT payload_json FROM created_records")
        .fetchone()[0]
    )
    assert payload["event_id"] == event.id
    assert payload["schema_version"] == 1

    events, rows = len(store.list_events(run_id)), count(store, "created_records")

    def broken(*args: object) -> None:
        raise RuntimeError("after insert")

    authorized(store, run_id, "create-2")
    events += 2
    with monkeypatch.context() as patch:
        patch.setattr(sql, "_select_one", broken)
        with pytest.raises(RuntimeError):
            store.record_created(creation(run_id, "create-2", system_identifier="43"))
    assert len(store.list_events(run_id)) == events
    assert count(store, "created_records") == rows


def test_duplicate_response_delivery_is_idempotent(history_context: ProjectContext) -> None:
    store = open_history(history_context)
    run_id = store.create_run(history_context, TextMission("create")).record.id
    authorized(store, run_id)

    first = store.record_created(creation(run_id))
    again = store.record_created(creation(run_id))

    assert again == first
    assert count(store, "created_records") == 1
    created = [e for e in store.list_events(run_id) if e.event_type == EventType.RECORD_CREATED]
    assert len(created) == 1
    with pytest.raises(InvalidEventError, match="ambiguous_creation"):
        store.record_created(creation(run_id, system_identifier="99"))
    authorized(store, run_id, "create-2")
    collision = store.record_created(creation(run_id, "create-2"))
    assert collision.warnings == ("identifier_collision",)


def test_creation_requires_authorized_action_of_same_run(history_context: ProjectContext) -> None:
    store = open_history(history_context)
    run_id = store.create_run(history_context, TextMission("create")).record.id
    record_call(store, run_id, "denied", "submit", {}, Decision("deny", "rule", ""))

    for action in ("never-called", "denied"):
        with pytest.raises(InvalidEventError):
            store.record_created(creation(run_id, action))
    assert count(store, "created_records") == 0


def test_unknown_identifier_is_stored_with_warning(history_context: ProjectContext) -> None:
    store = open_history(history_context)
    run_id = store.create_run(history_context, TextMission("create")).record.id
    authorized(store, run_id)

    record = store.record_created(creation(run_id, system_identifier=None))

    assert record.system_identifier == "unknown"
    assert not record.identified
    assert record.detail_path is None
    assert record.warnings == ("identifier_not_extracted",)
    assert unidentified_warning(run_id, [record]) == (
        "1 created record could not be identified automatically; "
        f"check the target system for names starting with LASTRO-{run_id[-4:]}."
    )
    assert unidentified_warning(run_id, []) is None


def test_history_failure_rolls_back_and_fails_closed(
    history_context: ProjectContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = open_history(history_context)
    run_id = store.create_run(history_context, TextMission("create")).record.id
    authorized(store, run_id)

    def fail(*args: object, **kwargs: object) -> None:
        raise HistoryUnavailableError()

    with monkeypatch.context() as patch:
        patch.setattr(Database, "write", fail)
        with pytest.raises(HistoryUnavailableError):
            store.record_created(creation(run_id, cleanup_instructions="token=fictional-value"))
    assert count(store, "created_records") == 0
    fallback = store.run_paths(run_id).run_dir / "fallback-events.jsonl"
    entry = json.loads(fallback.read_text(encoding="utf-8"))
    assert entry["event_type"] == "record_created"
    assert entry["payload"]["record_id"] == "42"
    assert "fictional-value" not in fallback.read_text(encoding="utf-8")
    with pytest.raises(HistoryUnavailableError):
        store.record_created(creation(run_id))


def test_provenance_rebuild_ignores_unknown_and_wrong_tenant(
    history_context: ProjectContext,
) -> None:
    store = open_history(history_context)
    run_id = store.create_run(history_context, TextMission("create")).record.id
    for action in ("a1", "a2", "a3"):
        authorized(store, run_id, action)
    store.record_created(creation(run_id, "a1", system_identifier=None))
    store.record_created(creation(run_id, "a2", system_identifier="77", tenant="other"))
    store.record_created(creation(run_id, "a3", system_identifier="78"))

    restored = Provenance(store, run_id, history_context.profile)

    assert restored.origin("employee", "unknown") == "unknown"
    assert restored.origin("employee", "77") == "unknown"
    assert restored.origin("employee", "78") == "created_in_run"
    assert restored.origin("employee", "") == "unknown"


def test_known_record_restores_created_origin(history_context: ProjectContext) -> None:
    store = open_history(history_context)
    run_id = store.create_run(history_context, TextMission("create")).record.id
    authorized(store, run_id)
    provenance = Provenance(store, run_id, history_context.profile)

    provenance.record(creation(run_id))
    provenance.record(creation(run_id))

    assert provenance.origin("employee", "42") == "created_in_run"
    restored = Provenance(store, run_id, history_context.profile)
    assert restored.origin("employee", "42") == "created_in_run"
    assert restored.origin("customer", "42") == "unknown"


def test_cleanup_transitions_respect_state_and_project(
    history_context: ProjectContext,
    clock_now: list[datetime],
    fake_clock: Callable[[], datetime],
) -> None:
    store = open_history(history_context, clock=fake_clock)
    run_id = store.create_run(history_context, TextMission("create")).record.id
    authorized(store, run_id, "a1")
    authorized(store, run_id, "a2")
    pending = store.record_created(creation(run_id, "a1"))
    skipped = store.record_created(
        creation(run_id, "a2", system_identifier="43", cleanup_required=False)
    )
    assert skipped.cleanup_status == CleanupStatus.NOT_REQUIRED
    assert [r.id for r in store.list_created_records(cleanup_status=CleanupStatus.PENDING)] == [
        pending.id
    ]

    clock_now[0] += timedelta(minutes=5)
    result = store.mark_created_record_cleaned(pending.id)
    assert result.changed
    assert result.record.cleanup_status == CleanupStatus.CLEANED_MANUALLY
    assert result.record.cleaned_at == "2026-10-30T17:37:05.000000Z"
    clock_now[0] += timedelta(minutes=5)
    repeated = store.mark_created_record_cleaned(pending.id)
    assert not repeated.changed
    assert repeated.record.cleaned_at == result.record.cleaned_at
    with pytest.raises(CleanupTransitionError):
        store.mark_created_record_cleaned(skipped.id)
    unchanged = {r.id: r for r in store.list_created_records(run_id=run_id)}[skipped.id]
    assert unchanged.cleaned_at is None
    with pytest.raises(CreatedRecordNotFoundError):
        store.mark_created_record_cleaned("created_missing")
    other = HistoryStore(
        store._layout,  # pyright: ignore[reportPrivateUsage]
        store._database,  # pyright: ignore[reportPrivateUsage]
        project_id="00000000-0000-4000-8000-000000000000",
        timezone="UTC",
    )
    assert other.list_created_records() == []
    with pytest.raises(CreatedRecordNotFoundError):
        other.mark_created_record_cleaned(skipped.id)
