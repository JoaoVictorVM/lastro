import asyncio
import sqlite3
from collections.abc import Callable
from typing import Any, cast

from playwright.async_api import Page

from lastro.browser.demo import FIELDS, demo_policy
from lastro.browser.tools import BrowserTools
from lastro.config.limits import RunLimits
from lastro.config.loader import ProjectContext
from lastro.config.policy import Preauthorized
from lastro.execution.models import stop_message
from lastro.execution.monitor import EXECUTION_LOCK
from lastro.history import EventType, RunStatus, TextMission, open_history

WRITE_FETCH = (
    "fetch('/employees/1', {method: 'POST', body: new URLSearchParams({email: 'x@y.z'})})"
    ".then(() => false).catch(() => true)"
)


def _writable(context: ProjectContext, **limits: int) -> ProjectContext:
    context.profile.policy = demo_policy()
    context.profile.policy.preauthorized_records = [
        Preauthorized(tenant="aurora", entity="employee", id=str(i)) for i in range(1, 101)
    ]
    context.profile.editable_fields = {"employee": list(FIELDS)}
    context.profile.run_limits = RunLimits(**limits)
    return context


def _start(context: ProjectContext) -> tuple[BrowserTools, str]:
    history = open_history(context)
    run = history.create_run(context, TextMission("Explore employees"))
    return BrowserTools(context, history, run.record.id), run.record.id


def _posts(demo_server: Any) -> list[tuple[str, str]]:
    return [
        (method, path)
        for method, path in demo_server.access_log
        if method == "POST" and path.startswith("/employees")
    ]


def test_action_150_locks_and_reports(make_auth_project: Callable[..., Any]) -> None:
    context = make_auth_project().context()
    context.profile.run_limits = RunLimits(max_no_progress_actions=500)
    runtime, run_id = _start(context)

    async def scenario() -> list[dict[str, Any]]:
        results = [
            await runtime.call("navigate", {"url": f"https://evil.example/{index}"})
            for index in range(151)
        ]
        await runtime.monitor.wait(15)
        return results

    results = asyncio.run(scenario())
    assert results[119]["execution_notice"]["kind"] == "warning"
    assert results[119]["execution_notice"]["used"] == 120
    assert results[149]["execution_notice"] == {
        "kind": "stopped",
        "status": "limit_reached",
        "reason": "action_limit",
    }
    assert results[150]["decision"] == "deny" and results[150]["reason"] == "write_lock"
    history = runtime.history
    state = history.execution_state(run_id)
    assert state.actions_reserved == 150
    record = history.get_run(run_id)
    assert (record.status, record.status_reason) == (RunStatus.LIMIT_REACHED, "action_limit")
    assert EXECUTION_LOCK in runtime.session.policy.locks
    snapshots = history.list_report_snapshots(run_id)
    assert [item.kind.value for item in snapshots] == ["partial"]
    assert stop_message("action_limit", 150, "report.partial.md") == (
        "Run stopped: action limit (150) reached. Partial report: report.partial.md"
    )


def test_invalid_and_denied_calls_count(make_auth_project: Callable[..., Any]) -> None:
    context = make_auth_project().context()
    runtime, run_id = _start(context)

    async def scenario() -> None:
        assert "error" in await runtime.call("click", {})
        denied = await runtime.call("navigate", {"url": "https://evil.example"})
        assert denied["reason"] == "outside_target"
        assert "error" in await runtime.call("knowledge_query", {"bogus": 1})

    asyncio.run(scenario())
    assert runtime.history.execution_state(run_id).actions_reserved == 3
    events = runtime.history.list_events(run_id)
    calls = [e for e in events if e.event_type == EventType.TOOL_CALL]
    decisions = [e for e in events if e.event_type == EventType.POLICY_DECISION and e.tool_name]
    # Uma chamada auditável gera exatamente um tool_call e um policy_decision.
    assert len(calls) == len(decisions) == 3


def test_status_and_end_are_exempt(make_auth_project: Callable[..., Any]) -> None:
    context = make_auth_project().context()
    runtime, run_id = _start(context)

    async def scenario() -> None:
        await runtime.call("approval_status", {"approval_id": "missing"})
        await runtime.call("end_run", {"outcome": "completed", "reason": "done"})

    asyncio.run(scenario())
    assert runtime.history.execution_state(run_id).actions_reserved == 0
    assert runtime.history.get_run(run_id).status == RunStatus.COMPLETED


def test_last_action_can_send_only_one_bound_request(
    make_auth_project: Callable[..., Any], demo_server: Any
) -> None:
    context = _writable(make_auth_project().context(), max_actions=3)
    runtime, run_id = _start(context)

    async def scenario() -> dict[str, Any]:
        await runtime.call("navigate", {"url": context.profile.target_url + "/employees/1/edit"})
        filled = await runtime.call(
            "fill", {"target": {"label": "E-mail"}, "value": "qa-last@example.com"}
        )
        assert filled["status"] == "ok", filled
        submitted = await runtime.call("submit", {"target": {"role": "button", "name": "Salvar"}})
        # Depois da escrita única, o lock já está engajado antes de qualquer outra.
        assert EXECUTION_LOCK in runtime.session.policy.locks
        await runtime.monitor.wait(15)
        return submitted

    submitted = asyncio.run(scenario())
    assert submitted["status"] == "ok", submitted
    assert _posts(demo_server) == [("POST", "/employees/1")]
    record = runtime.history.get_run(run_id)
    assert (record.status, record.status_reason) == (RunStatus.LIMIT_REACHED, "action_limit")
    (intent,) = runtime.history.list_write_intents(run_id)
    assert intent.state.value == "response_known" and intent.response_status == 200


def test_write_after_limit_never_reaches_demo(
    make_auth_project: Callable[..., Any], demo_server: Any
) -> None:
    context = _writable(make_auth_project().context())
    runtime, run_id = _start(context)

    async def scenario() -> bool:
        try:
            await runtime.call("navigate", {"url": context.profile.target_url + "/employees"})
            # Outro processo grava o latch: o guard consulta o estado durável antes da rede.
            open_history(context).latch_stop(run_id, RunStatus.LIMIT_REACHED, "no_progress")
            page = cast(Page, runtime.session.page)
            blocked = await page.evaluate(WRITE_FETCH)
            return bool(blocked)
        finally:
            await runtime.session.close()

    assert asyncio.run(scenario())
    assert _posts(demo_server) == []
    assert runtime.session.network_denial == "write_lock"


def test_deadline_rechecked_at_network_boundary(
    make_auth_project: Callable[..., Any], demo_server: Any
) -> None:
    context = _writable(make_auth_project().context())
    runtime, run_id = _start(context)

    async def scenario() -> bool:
        try:
            await runtime.call("navigate", {"url": context.profile.target_url + "/employees"})
            runtime.monitor.arm()
            runtime.monitor.deadline = runtime.monitor.monotonic() - 1
            page = cast(Page, runtime.session.page)
            return bool(await page.evaluate(WRITE_FETCH))
        finally:
            await runtime.monitor.wait(15)
            await runtime.session.close()

    assert asyncio.run(scenario())
    assert _posts(demo_server) == []
    state = runtime.history.execution_state(run_id)
    assert (state.stop_status, state.stop_reason) == ("limit_reached", "duration_limit")


def test_cancel_revokes_last_action_grant(make_auth_project: Callable[..., Any]) -> None:
    from lastro.browser.bindings import ExpectedRequest

    context = _writable(make_auth_project().context(), max_actions=1)
    runtime, run_id = _start(context)
    reservation = runtime.history.reserve_action(run_id, "last", "submit")
    assert reservation.last
    runtime.monitor.admitted("submit", "last", True)
    assert runtime.monitor.grant == "last"
    binding = context.profile.policy.bindings[1]
    expected = ExpectedRequest(binding, "http://x/employees/1", {}, "last")

    async def scenario() -> str | None:
        assert runtime.monitor.check_write(expected) is None
        runtime.history.request_cancellation(run_id)
        verdict = runtime.monitor.check_write(expected)
        await runtime.monitor.wait(15)
        return verdict

    assert asyncio.run(scenario()) == "write_lock"
    assert runtime.monitor.grant is None
    with sqlite3.connect(runtime.history.database_path) as conn:
        status = conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()[0]
    assert status == "cancelled"
