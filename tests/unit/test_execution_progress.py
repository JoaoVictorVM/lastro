from collections.abc import Callable

from lastro.config.loader import ProjectContext
from lastro.execution.models import ActionOutcome
from lastro.execution.progress import event_keys, new_keys, page_key
from lastro.history import EventType, RunStatus, TextMission, open_history
from lastro.history.store import HistoryStore

PAGE = "http://127.0.0.1:8765/employees"


def _start(make: Callable[[str], ProjectContext]) -> tuple[HistoryStore, str]:
    context = make("homolog")
    history = open_history(context)
    run = history.create_run(context, TextMission("Explore employees"))
    return history, run.record.id


def _act(history: HistoryStore, run_id: str, index: int, page: str | None = PAGE) -> ActionOutcome:
    action_id = f"a{index}"
    history.reserve_action(run_id, action_id, "observe")
    return history.complete_action(run_id, action_id, page=page_key(page) if page else None)


def test_controller_and_noise_events_are_not_progress() -> None:
    # Taxonomia fixa: eventos do controle não reiniciam o detector.
    for kind in ("limit_hit", "execution_warning", "cancellation_requested", "run_ended"):
        assert event_keys(kind, {}) == []
    assert event_keys("notification_degraded", {}) == []
    assert event_keys("knowledge_proposed", {"subtype": "duplicate_conflict"}) == []
    assert event_keys("finding_submitted", {"finding_id": "f1"}) == [
        "type:finding_submitted",
        "finding:f1",
    ]


def test_only_first_event_type_is_new() -> None:
    seen: set[str] = set()
    first = new_keys(seen, [("tool_call", {"text": "a"}), ("policy_decision", {})], None)
    assert first == ["type:tool_call", "type:policy_decision"]
    seen |= set(first)
    # Resultado textual novo não é novidade: só o tipo conta, uma vez por run.
    assert new_keys(seen, [("tool_call", {"text": "b"}), ("oracle_signal", {})], None) == [
        "type:oracle_signal"
    ]


def test_page_key_ignores_query_fragment_and_trailing_slash() -> None:
    assert page_key(PAGE + "?utm=1#top") == page_key(PAGE + "/")
    assert page_key("about:blank") is None


def test_ten_actions_without_progress_stop(
    history_context_for: Callable[[str], ProjectContext],
) -> None:
    history, run_id = _start(history_context_for)
    _act(history, run_id, 0)
    for index in range(1, 10):
        outcome = _act(history, run_id, index)
        assert outcome.stop_reason is None
    outcome = _act(history, run_id, 10)
    assert outcome.stop_status == "limit_reached"
    assert outcome.stop_reason == "no_progress"
    hits = [e for e in history.list_events(run_id) if e.event_type == EventType.LIMIT_HIT]
    assert len(hits) == 1
    assert isinstance(hits[0].payload, dict)
    assert hits[0].payload["dimension"] == "no_progress"
    assert hits[0].payload["observed"] == 10


def test_repeated_page_and_rejected_proposal_are_not_progress(
    history_context_for: Callable[[str], ProjectContext],
) -> None:
    history, run_id = _start(history_context_for)
    _act(history, run_id, 0)
    history.record_knowledge_rejection(run_id, "invalid_fact", {"x": 1})
    outcome = _act(history, run_id, 1, PAGE + "?page=2")
    assert not outcome.progressed
    assert history.execution_state(run_id).no_progress_count == 1


def test_new_accepted_finding_resets_streak(
    history_context_for: Callable[[str], ProjectContext],
) -> None:
    history, run_id = _start(history_context_for)
    for index in range(5):
        _act(history, run_id, index)
    assert history.execution_state(run_id).no_progress_count == 4
    history.append_event(run_id, EventType.FINDING_SUBMITTED, payload={"finding_id": "f-1"})
    assert _act(history, run_id, 5).progressed
    assert history.execution_state(run_id).no_progress_count == 0
    # O mesmo finding repetido não reinicia de novo.
    history.append_event(run_id, EventType.FINDING_SUBMITTED, payload={"finding_id": "f-1"})
    assert not _act(history, run_id, 6).progressed
    # Página nova observada pelo browser é novidade.
    assert _act(history, run_id, 7, PAGE + "/1/edit").progressed
    history.finish_run(run_id, RunStatus.COMPLETED)
