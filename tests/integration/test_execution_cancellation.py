import asyncio
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from lastro.browser.demo import FIELDS, demo_policy
from lastro.browser.tools import BrowserTools
from lastro.config.loader import ProjectContext
from lastro.execution.monitor import EXECUTION_LOCK
from lastro.execution.supervisor import stop_run
from lastro.history import (
    ApprovalStatus,
    EventType,
    HistoryError,
    RunStatus,
    TextMission,
    open_history,
)


def _writable(context: ProjectContext) -> ProjectContext:
    context.profile.policy = demo_policy()
    context.profile.editable_fields = {"employee": list(FIELDS)}
    return context


def _start(context: ProjectContext) -> tuple[BrowserTools, str]:
    history = open_history(context)
    run = history.create_run(context, TextMission("Explore employees"))
    return BrowserTools(context, history, run.record.id), run.record.id


def _cancel_in_thread(context: ProjectContext, run_id: str) -> tuple[threading.Thread, list[Any]]:
    """A CLI roda em outro processo; aqui, em outra thread com outra fachada."""
    found: list[Any] = []

    def run() -> None:
        found.append(stop_run(open_history(context), run_id))

    thread = threading.Thread(target=run)
    thread.start()
    return thread, found


def _alive(runtime: BrowserTools, monkeypatch: pytest.MonkeyPatch) -> None:
    # O owner deste teste é o próprio processo: a CLI o considera vivo e espera por ele.
    from lastro.execution import supervisor

    monkeypatch.setattr(supervisor, "owner_alive", lambda store, record: True)


def test_cancel_finishes_within_fifteen_seconds(
    make_auth_project: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    context = make_auth_project().context()
    runtime, run_id = _start(context)
    _alive(runtime, monkeypatch)

    async def scenario() -> tuple[float, Any]:
        await runtime.monitor.start()
        await runtime.call("navigate", {"url": context.profile.target_url + "/employees"})
        started = time.monotonic()
        thread, found = _cancel_in_thread(context, run_id)
        while thread.is_alive():
            await asyncio.sleep(0.05)
        await runtime.monitor.close()
        return time.monotonic() - started, found[0]

    elapsed, outcome = asyncio.run(scenario())
    assert elapsed < 15
    assert (outcome.status, outcome.reason, outcome.finished_by) == (
        "cancelled",
        "user_cancelled",
        "owner",
    )
    assert outcome.report_kind == "partial" and outcome.report_path is not None
    assert outcome.report_path.exists()
    assert runtime.session.page is None


def test_inflight_response_drains_for_at_most_ten_seconds(
    make_auth_project: Callable[..., Any],
) -> None:
    context = make_auth_project().context()
    runtime, run_id = _start(context)

    async def scenario() -> float:
        await runtime.call("navigate", {"url": context.profile.target_url + "/employees"})

        async def stuck(*_: object) -> None:
            await asyncio.sleep(60)

        # Uma resposta pendurada nunca prende o encerramento além da drenagem.
        task = asyncio.create_task(stuck())
        runtime.session.response_tasks.add(task)
        started = time.monotonic()
        runtime.monitor.trigger(RunStatus.CANCELLED.value, "user_cancelled")
        await runtime.monitor.wait(20)
        return time.monotonic() - started

    elapsed = asyncio.run(scenario())
    assert elapsed < 15
    assert runtime.history.get_run(run_id).status == RunStatus.CANCELLED


def test_cancel_does_not_wait_for_session_lock(make_auth_project: Callable[..., Any]) -> None:
    context = make_auth_project().context()
    runtime, run_id = _start(context)

    async def scenario() -> None:
        await runtime.session.lock.acquire()
        try:
            runtime.history.request_cancellation(run_id)
            runtime.monitor.tick()
            # O lock de escrita entra antes de qualquer espera pelo lock da sessão.
            assert EXECUTION_LOCK in runtime.session.policy.locks
            assert runtime.history.get_run(run_id).status == RunStatus.RUNNING
        finally:
            runtime.session.lock.release()
        await runtime.monitor.wait(15)

    asyncio.run(scenario())
    assert runtime.history.get_run(run_id).status == RunStatus.CANCELLED


def test_cancel_pending_and_approved_unconsumed_gates(
    make_auth_project: Callable[..., Any], demo_server: Any
) -> None:
    context = _writable(make_auth_project().context())
    runtime, run_id = _start(context)

    async def scenario() -> None:
        await runtime.call("navigate", {"url": context.profile.target_url + "/employees/1/edit"})
        result = await runtime.call(
            "fill", {"target": {"label": "E-mail"}, "value": "gate@example.com"}
        )
        assert result["decision"] == "awaiting_approval", result
        runtime.history.request_cancellation(run_id)
        runtime.monitor.tick()
        await runtime.monitor.wait(15)

    asyncio.run(scenario())
    history = runtime.history
    (approval,) = history.list_approvals(run_id)
    assert approval.status == ApprovalStatus.SKIPPED
    assert history.get_run(run_id).status == RunStatus.CANCELLED
    pending = history.pending_items(run_id)
    assert ("approval", "withdrawn") in {(item.kind, item.state) for item in pending}
    decided = [e for e in history.list_events(run_id) if e.event_type == EventType.APPROVAL_DECIDED]
    assert decided and isinstance(decided[-1].payload, dict)
    assert decided[-1].payload["reason"] == "cancelled"
    assert not [
        path
        for method, path in demo_server.access_log
        if method == "POST" and path.startswith("/employees")
    ]


def test_cancel_races_with_end_and_cost_cap(make_auth_project: Callable[..., Any]) -> None:
    context = make_auth_project().context()
    runtime, run_id = _start(context)
    history = runtime.history
    first = history.latch_stop(run_id, RunStatus.LIMIT_REACHED, "cost_cap")
    second = history.request_cancellation(run_id)
    assert first.won and not second.won

    async def scenario() -> None:
        runtime.monitor.tick()
        await runtime.monitor.wait(15)
        ended = await runtime.call("end_run", {"outcome": "completed", "reason": "late"})
        assert ended.get("already_ended") or "error" in ended

    asyncio.run(scenario())
    record = history.get_run(run_id)
    assert (record.status, record.status_reason) == (RunStatus.LIMIT_REACHED, "cost_cap")
    ended = [e for e in history.list_events(run_id) if e.event_type == EventType.RUN_ENDED]
    assert len(ended) == 1


def test_report_timeout_uses_json_fallback(
    make_auth_project: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from lastro.reporting.builder import ReportBuilder

    context = make_auth_project().context()
    runtime, run_id = _start(context)

    def broken(self: ReportBuilder, data: Any) -> str:
        raise RuntimeError("template exploded")

    monkeypatch.setattr(ReportBuilder, "render_report", broken)

    async def scenario() -> dict[str, Any] | None:
        runtime.monitor.trigger(RunStatus.CANCELLED.value, "user_cancelled")
        return await runtime.monitor.wait(15)

    result = asyncio.run(scenario())
    assert result is not None
    assert result["report_status"] == "fallback"
    assert str(result["report_path"]).endswith("report.partial.json")
    summary = runtime.history.execution_summary(run_id)
    assert summary.report_path == "report.partial.json"


def test_history_failure_never_claims_success(
    make_auth_project: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from lastro.history.store import HistoryStore

    context = make_auth_project().context()
    history = open_history(context)
    run_id = history.create_run(context, TextMission("Explore")).record.id

    def unavailable(self: HistoryStore, *args: object, **kwargs: object) -> object:
        raise HistoryError("history unavailable")

    monkeypatch.setattr(HistoryStore, "latch_stop", unavailable)
    with pytest.raises(HistoryError):
        stop_run(history, run_id)
    assert history.get_run(run_id).status == RunStatus.RUNNING


def test_request_survives_second_interrupt(
    make_auth_project: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from lastro.execution import supervisor

    context = make_auth_project().context()
    history = open_history(context)
    run_id = history.create_run(context, TextMission("Explore")).record.id
    monkeypatch.setattr(supervisor, "owner_alive", lambda store, record: True)

    def interrupt(_: float) -> None:
        raise KeyboardInterrupt

    # Segundo Ctrl+C só abandona a espera: o pedido durável continua valendo.
    with pytest.raises(KeyboardInterrupt):
        stop_run(history, run_id, sleep=interrupt)
    state = history.execution_state(run_id)
    assert (state.stop_status, state.stop_reason) == ("cancelled", "user_cancelled")
    assert history.get_run(run_id).status == RunStatus.RUNNING


def test_cancel_without_owner_is_finished_by_supervisor(
    make_auth_project: Callable[..., Any],
) -> None:
    context = make_auth_project().context()
    history = open_history(context)
    run_id = history.create_run(context, TextMission("Explore")).record.id
    outcome = stop_run(history, run_id)
    assert (outcome.status, outcome.finished_by) == ("cancelled", "supervisor")
    assert outcome.report_path is not None and outcome.report_path.exists()
    again = stop_run(history, run_id)
    assert again.already_ended and again.report_path == outcome.report_path


def test_cancel_invalidates_approved_unconsumed_gate(
    make_auth_project: Callable[..., Any],
) -> None:
    context = _writable(make_auth_project().context())
    runtime, run_id = _start(context)

    async def scenario() -> None:
        await runtime.call("navigate", {"url": context.profile.target_url + "/employees/1/edit"})
        result = await runtime.call(
            "fill", {"target": {"label": "E-mail"}, "value": "gate@example.com"}
        )
        assert result["decision"] == "awaiting_approval", result
        runtime.history.decide_approval(result["approval_id"], "approve", "cli", run_id=run_id)
        runtime.history.request_cancellation(run_id)
        runtime.monitor.tick()
        await runtime.monitor.wait(15)

    asyncio.run(scenario())
    (approval,) = runtime.history.list_approvals(run_id)
    # A aprovação humana continua registrada; só perdeu a autoridade para executar.
    assert approval.status == ApprovalStatus.INVALIDATED
    assert approval.decided_by is not None and approval.decided_by.value == "cli"
    kinds = {(item.kind, item.state) for item in runtime.history.pending_items(run_id)}
    assert ("approval", "withdrawn") in kinds
