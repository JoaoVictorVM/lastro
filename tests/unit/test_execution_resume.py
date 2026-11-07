from lastro.execution.models import ReconciliationState, WriteIntent, WriteState
from lastro.execution.resume import (
    RESUME_RECORD_MISSING,
    RESUME_STATE_CONFLICT,
    RESUME_UNVERIFIABLE,
    classify,
    missing_record_message,
)


def _intent(**changes: object) -> WriteIntent:
    data: dict[str, object] = {
        "id": "wi_1",
        "run_id": "run_a",
        "action_id": "a1",
        "binding_id": "employee-update",
        "operation": "update",
        "entity": "employee",
        "record_id": "57",
        "tenant": "aurora",
        "detail_path": "/employees/57/edit",
        "before": {"email": "old@example.com", "phone": "1"},
        "intended": {"email": "new@example.com", "phone": "1"},
        "comparable": True,
        "state": WriteState.RESPONSE_UNKNOWN,
        "response_status": None,
        "created_at": "2026-11-05T10:00:00.000000Z",
        "sent_at": "2026-11-05T10:00:01.000000Z",
        "resolved_at": None,
        **changes,
    }
    return WriteIntent(**data)  # type: ignore[arg-type]


def test_exact_values_are_already_applied() -> None:
    result = classify(_intent(), {"email": "new@example.com", "phone": "1", "role": "x"})
    assert result.state == ReconciliationState.ALREADY_APPLIED and result.code is None


def test_previous_values_are_ready_for_review() -> None:
    result = classify(_intent(), {"email": "old@example.com", "phone": "1"})
    assert result.state == ReconciliationState.READY_FOR_REVIEW


def test_partial_change_is_conflict() -> None:
    result = classify(_intent(), {"email": "other@example.com", "phone": "1"})
    assert (result.state, result.code) == (ReconciliationState.CONFLICT, RESUME_STATE_CONFLICT)
    # Sem estado anterior conhecido, valor diferente nunca vira pronto para reenvio.
    unknown_before = classify(_intent(before=None), {"email": "old@example.com", "phone": "1"})
    assert unknown_before.state == ReconciliationState.CONFLICT


def test_redacted_values_are_not_comparable() -> None:
    result = classify(_intent(comparable=False), {"email": "new@example.com", "phone": "1"})
    assert (result.state, result.code) == (ReconciliationState.INCONCLUSIVE, RESUME_UNVERIFIABLE)
    unreadable = classify(_intent(), {"email": None, "phone": "1"})
    assert unreadable.code == RESUME_UNVERIFIABLE


def test_missing_record_is_inconclusive() -> None:
    result = classify(_intent(), None)
    assert (result.state, result.code) == (ReconciliationState.INCONCLUSIVE, RESUME_RECORD_MISSING)
    assert result.reason == "Record 57 not found on resume; no write repeated."
    assert missing_record_message(None) == "Record unknown not found on resume; no write repeated."
