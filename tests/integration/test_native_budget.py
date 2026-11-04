import threading
from typing import Any

import pytest

from lastro.history import BudgetError, BudgetExceededError, Settlement


def detach_active(history: Any) -> None:
    """Outro worker da mesma suíte: a run anterior continua running, sem ponteiro ativo."""
    history._layout.active_run.unlink()  # pyright: ignore[reportPrivateUsage]


def settlement(actual: int, tokens: int = 100) -> Settlement:
    return Settlement(
        input_tokens=tokens,
        cache_read_tokens=0,
        cache_write_tokens=0,
        output_tokens=10,
        actual_nusd=actual,
        request_id="req_1",
        latency_ms=5,
        tool_calls=(("toolu_1", "observe"),),
        pricing_id="sha256:" + "b" * 64,
    )


def test_atomic_run_and_suite_reservations(native_harness: Any) -> None:
    history = native_harness.history()
    suite = history.create_suite_budget(1000)
    run_a = native_harness.start(history, cap_nusd=800, suite=suite)
    history.reserve_model_call(native_harness.reservation(run_a, 600))
    with pytest.raises(BudgetExceededError) as run_cap:
        history.reserve_model_call(native_harness.reservation(run_a, 300, step=2))
    assert run_cap.value.scope == "run"
    detach_active(history)
    run_b = native_harness.start(history, cap_nusd=800, suite=suite)
    with pytest.raises(BudgetExceededError) as suite_cap:
        history.reserve_model_call(native_harness.reservation(run_b, 500))
    assert suite_cap.value.scope == "suite"
    assert [call.run_id for call in history.list_model_calls(run_b)] == []
    history.reserve_model_call(native_harness.reservation(run_b, 400))


def test_concurrent_workers_cannot_oversubscribe_suite(native_harness: Any) -> None:
    history = native_harness.history()
    suite = history.create_suite_budget(1000)
    runs: list[str] = []
    for _ in range(4):
        runs.append(native_harness.start(history, suite=suite))
        detach_active(history)
    accepted: list[str] = []
    rejected: list[str] = []
    barrier = threading.Barrier(len(runs))

    def worker(run_id: str) -> None:
        store = native_harness.history()
        barrier.wait()
        try:
            store.reserve_model_call(native_harness.reservation(run_id, 300))
            accepted.append(run_id)
        except BudgetExceededError:
            rejected.append(run_id)

    threads = [threading.Thread(target=worker, args=(run_id,)) for run_id in runs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(accepted) == 3
    assert len(rejected) == 1
    committed = sum(
        call.committed_nusd for run_id in runs for call in history.list_model_calls(run_id)
    )
    assert committed == 900


def test_exact_cap_boundary(native_harness: Any) -> None:
    history = native_harness.history()
    run_id = native_harness.start(history, cap_nusd=1000)
    history.reserve_model_call(native_harness.reservation(run_id, 1000))
    with pytest.raises(BudgetExceededError):
        history.reserve_model_call(native_harness.reservation(run_id, 1, step=2))


def test_duplicate_settlement_is_idempotent(native_harness: Any) -> None:
    history = native_harness.history()
    run_id = native_harness.start(history)
    call = history.reserve_model_call(native_harness.reservation(run_id, 5000))
    with pytest.raises(BudgetError):
        history.reserve_model_call(native_harness.reservation(run_id, 5000))
    history.mark_model_call_sent(call.id)
    first = history.settle_model_call(call.id, settlement(1200))
    second = history.settle_model_call(call.id, settlement(1200))
    assert first == second
    completed = [e for e in history.list_events(run_id) if e.event_type == "model_call_completed"]
    assert len(completed) == 1
    assert completed[0].id == first.completion_event_id
    usage = history.native_usage(run_id)
    assert usage.confirmed_nusd == 1200
    assert usage.committed_nusd == 1200


def test_sent_timeout_retains_reservation(native_harness: Any) -> None:
    history = native_harness.history()
    run_id = native_harness.start(history, cap_nusd=10_000)
    unsent = history.reserve_model_call(native_harness.reservation(run_id, 4000))
    released = history.fail_model_call(unsent.id, released=False, error_code="cancelled")
    assert released.status == "released"
    sent = history.reserve_model_call(native_harness.reservation(run_id, 6000, step=2))
    history.mark_model_call_sent(sent.id)
    failed = history.fail_model_call(sent.id, released=False, error_code="timeout")
    assert failed.status == "uncertain"
    assert failed.actual_nusd is None
    usage = history.native_usage(run_id)
    assert usage.committed_nusd == 6000
    assert usage.confirmed_nusd == 0
    assert usage.uncertain_calls == 1
    with pytest.raises(BudgetExceededError):
        history.reserve_model_call(native_harness.reservation(run_id, 4001, step=3))
    events = [e.event_type for e in history.list_events(run_id)]
    assert events.count("model_call_failed") == 1


def test_actual_above_reservation_is_accounting_failure(native_harness: Any) -> None:
    from lastro.native.budget import BudgetAccountingViolation, BudgetLedger
    from lastro.native.pricing import Usage

    history = native_harness.history()
    run_id = native_harness.start(history)
    ledger = BudgetLedger(history, run_id)
    call = ledger.reserve(
        step_index=1,
        attempt=1,
        node="decide",
        model="claude-sonnet-5-5",
        payload_sha256="d" * 64,
        max_output_tokens=256,
        reserved_nusd=10,
    )
    ledger.mark_sent(call.id)
    with pytest.raises(BudgetAccountingViolation):
        ledger.settle(
            call,
            Usage(1000, 0, 0, 100),
            request_id="req_x",
            latency_ms=1,
            tool_calls=(),
        )
    stored = history.list_model_calls(run_id)[0]
    assert stored.status == "settled"
    assert stored.actual_nusd is not None and stored.actual_nusd > stored.reserved_nusd
    with pytest.raises(BudgetError):
        ledger.reserve(
            step_index=2,
            attempt=1,
            node="decide",
            model="claude-sonnet-5-5",
            payload_sha256="d" * 64,
            max_output_tokens=256,
            reserved_nusd=1,
        )
