import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

import pytest

from lastro.config.loader import ProjectContext
from lastro.execution import WriteIntentInput, WriteState
from lastro.history import EventType, RunStatus, TextMission, open_history
from lastro.history.store import HistoryStore


def _start(context: ProjectContext, **kwargs: object) -> tuple[HistoryStore, str]:
    history = open_history(context, **kwargs)  # type: ignore[arg-type]
    run = history.create_run(context, TextMission("Explore employees"))
    return history, run.record.id


def _intent(run_id: str, action_id: str, **extra: object) -> WriteIntentInput:
    data: dict[str, object] = {
        "run_id": run_id,
        "action_id": action_id,
        "binding_id": "employee-update",
        "operation": "update",
        "entity": "employee",
        "record_id": "7",
        "tenant": "t1",
        "detail_path": "/employees/7/edit",
        "before": {"email": "old@example.com"},
        "intended": {"email": "new@example.com"},
        "comparable": True,
        **extra,
    }
    return WriteIntentInput(**data)  # type: ignore[arg-type]


def test_reservations_are_atomic(history_context: ProjectContext) -> None:
    history, run_id = _start(history_context)
    errors: list[BaseException] = []

    def reserve(index: int) -> None:
        try:
            open_history(history_context).reserve_action(run_id, f"a{index}", "observe")
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=reserve, args=(index,)) for index in range(45)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    state = history.execution_state(run_id)
    # O perfil homolog limita a 40: as reservas excedentes não consomem unidade.
    assert state.actions_reserved == 40
    with sqlite3.connect(history.database_path) as conn:
        ordinals = [row[0] for row in conn.execute("SELECT ordinal FROM execution_actions")]
    assert sorted(ordinals) == list(range(1, 41))
    refused = history.reserve_action(run_id, "late", "observe")
    assert not refused.admitted
    assert history.execution_state(run_id).actions_reserved == 40


def test_first_stop_reason_wins(history_context: ProjectContext) -> None:
    history, run_id = _start(history_context)
    first = history.latch_stop(run_id, RunStatus.LIMIT_REACHED, "duration_limit")
    second = history.request_cancellation(run_id)
    assert first.won and not second.won
    state = history.execution_state(run_id)
    assert (state.stop_status, state.stop_reason) == ("limit_reached", "duration_limit")
    events = [event.event_type for event in history.list_events(run_id)]
    assert events.count(EventType.LIMIT_HIT) == 1
    # O sinal concorrente fica auditado sem substituir o motivo.
    assert EventType.CANCELLATION_REQUESTED in events
    with sqlite3.connect(history.database_path) as conn, pytest.raises(sqlite3.DatabaseError):
        conn.execute(
            "UPDATE execution_controls SET stop_reason = 'user_cancelled' WHERE run_id = ?",
            (run_id,),
        )
    assert state.drain_deadline_at is not None and state.finalize_deadline_at is not None


def test_cancellation_request_is_idempotent(history_context: ProjectContext) -> None:
    history, run_id = _start(history_context)
    assert history.request_cancellation(run_id).won
    assert not history.request_cancellation(run_id).won
    cancels = [
        event
        for event in history.list_events(run_id)
        if event.event_type == EventType.CANCELLATION_REQUESTED
    ]
    assert len(cancels) == 1


def test_crash_after_sending_is_ambiguous(history_context: ProjectContext) -> None:
    history, run_id = _start(history_context)
    history.reserve_action(run_id, "a1", "submit")
    intent = history.prepare_write(_intent(run_id, "a1"))
    assert intent.state == WriteState.PREPARED and intent.comparable
    history.mark_write_sending(intent.id)
    # Processo morre antes da resposta: a recuperação marca efeito desconhecido.
    reopened = open_history(history_context)
    unknown = reopened.mark_unknown_writes(run_id)
    assert [item.state for item in unknown] == [WriteState.RESPONSE_UNKNOWN]
    pending = reopened.pending_items(run_id)
    assert [(item.kind, item.state) for item in pending if item.kind == "write"] == [
        ("write", "response_unknown")
    ]
    assert all(item.requires_manual_review for item in pending if item.kind == "write")


def test_redacted_intent_is_not_comparable(history_context: ProjectContext) -> None:
    history, run_id = _start(history_context)
    history.reserve_action(run_id, "a1", "submit")
    intent = history.prepare_write(
        _intent(run_id, "a1", intended={"email": "x", "api_key": "sk-ant-api03-" + "a" * 40})
    )
    assert not intent.comparable


def test_warning_once_at_eighty_percent_of_actions(history_context: ProjectContext) -> None:
    history, run_id = _start(history_context)
    warnings = [
        history.reserve_action(run_id, f"a{index}", "observe").warning for index in range(1, 41)
    ]
    issued = [(index + 1, item) for index, item in enumerate(warnings) if item is not None]
    assert [index for index, _ in issued] == [32]
    notice = issued[0][1]
    assert notice is not None
    assert notice.as_json() == {
        "kind": "warning",
        "dimension": "actions",
        "used": 32,
        "limit": 40,
        "message": "80% of action budget used; wrap up.",
    }
    events = [e for e in history.list_events(run_id) if e.event_type == EventType.EXECUTION_WARNING]
    assert len(events) == 1


def test_duration_warning_uses_started_at(
    history_context: ProjectContext,
    clock_now: list[datetime],
    fake_clock: Callable[[], datetime],
) -> None:
    history, run_id = _start(history_context, clock=fake_clock)
    clock_now[0] += timedelta(minutes=23)
    assert history.warn_duration(run_id) is None
    clock_now[0] += timedelta(minutes=1)
    notice = history.warn_duration(run_id)
    assert notice is not None and notice.dimension == "duration" and notice.used == 24
    assert history.warn_duration(run_id) is None
