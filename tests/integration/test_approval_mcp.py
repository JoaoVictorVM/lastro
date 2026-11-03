import asyncio
from collections.abc import Callable, Coroutine, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from playwright.async_api import Page

from lastro.approvals.notifications import (
    NotificationAdapter,
    NotificationMessage,
    NotificationRouter,
    SendResult,
)
from lastro.browser.demo import FIELDS, demo_policy
from lastro.browser.tools import BrowserTools
from lastro.config.loader import ProjectContext
from lastro.history import (
    ApprovalStatus,
    EventType,
    HistoryStore,
    NotificationChannel,
    NotificationStatus,
    RunStatus,
    TextMission,
    open_history,
)

type Scenario = Callable[[BrowserTools, HistoryStore, str, str], Coroutine[Any, Any, None]]


def demo_project(make_auth_project: Callable[..., Any]) -> ProjectContext:
    project = make_auth_project().context()
    project.profile.policy = demo_policy()
    project.profile.editable_fields = {"employee": list(FIELDS)}
    return project


def run_scenario(
    project: ProjectContext,
    scenario: Scenario,
    clock: Callable[[], datetime] | None = None,
    adapters: Sequence[NotificationAdapter] = (),
) -> None:
    history = open_history(project, clock=clock) if clock is not None else open_history(project)
    run_id = history.create_run(project, TextMission("Edit employee with approval")).record.id
    notifier = NotificationRouter(history, adapters) if adapters else None
    runtime = BrowserTools(project, history, run_id, notifier=notifier)

    async def main() -> None:
        try:
            await runtime.call("navigate", {"url": project.profile.target_url + "/employees"})
            page = cast(Page, runtime.session.page)
            href = await page.get_by_role("link", name="Editar", exact=True).first.get_attribute(
                "href"
            )
            assert href is not None
            await runtime.call("navigate", {"url": project.profile.target_url + href})
            await scenario(runtime, history, run_id, href)
        finally:
            runtime.approvals.close()
            await runtime.session.close()

    asyncio.run(main())


def writes(demo_server: Any, href: str) -> list[tuple[str, str]]:
    record_path = href.removesuffix("/edit")
    return [(m, p) for m, p in demo_server.access_log if m == "POST" and p == record_path]


EMAIL = {"target": {"label": "E-mail"}, "value": "qa@example.com", "reason": "Check e-mail"}
SAVE = {"target": {"role": "button", "name": "Salvar"}}


def test_require_approval_persists_before_effect(
    make_auth_project: Callable[..., Any], demo_server: Any
) -> None:
    async def scenario(runtime: BrowserTools, history: HistoryStore, run_id: str, href: str):
        page = cast(Page, runtime.session.page)
        before = await page.get_by_label("E-mail", exact=True).input_value()

        first = await runtime.call("fill", EMAIL)

        assert first["decision"] == "awaiting_approval"
        assert first["command"] == "lastro approve " + first["approval_id"]
        assert first["approval_id"] == first["action_id"]
        assert await page.get_by_label("E-mail", exact=True).input_value() == before
        record = history.get_approval(first["approval_id"], run_id)
        assert record is not None and record.status == ApprovalStatus.PENDING
        assert record.payload["changes"] == [
            {"field": "email", "old": before, "new": "qa@example.com"}
        ]
        assert record.payload["reason"] == "Check e-mail"
        assert record.payload["tenant"] == {"id": "aurora", "display": "Clínica Aurora"}
        assert history.get_run(run_id).status == RunStatus.AWAITING_APPROVAL

        blocked = await runtime.call("observe", {})
        assert (blocked["decision"], blocked["reason"]) == ("awaiting_approval", "approval_pending")
        forged = await runtime.call(
            "fill", {**EMAIL, "approval_id": first["approval_id"], "approval_proof": "opaque.v1.x"}
        )
        assert forged["reason"] == "invalid_approval_proof"
        context = await runtime.call("mission_context", {})
        assert context["run_id"] == run_id
        assert writes(demo_server, href) == []
        assert all(
            "opaque" not in str(event.arguments_preview) for event in history.list_events(run_id)
        )

    run_scenario(demo_project(make_auth_project), scenario)


def test_cli_approval_executes_retry_exactly_once(
    make_auth_project: Callable[..., Any], demo_server: Any
) -> None:
    async def scenario(runtime: BrowserTools, history: HistoryStore, run_id: str, href: str):
        filled = await runtime.call("fill", EMAIL)
        history.decide_approval(filled["approval_id"], "approve", "cli")
        status = await runtime.call(
            "approval_status", {"approval_id": filled["approval_id"], "wait_seconds": 0}
        )
        assert (status["status"], status["decided_by"]) == ("approved", "cli")
        done = await runtime.call("fill", {**EMAIL, "approval_id": filled["approval_id"]})
        assert (done["status"], done["approval_status"]) == ("ok", "consumed")
        replay = await runtime.call("fill", {**EMAIL, "approval_id": filled["approval_id"]})
        assert (replay["decision"], replay["approval_status"]) == ("not_executed", "consumed")

        saved = await runtime.call("submit", SAVE)
        assert saved["decision"] == "awaiting_approval"
        assert "(email) in Clínica Aurora" in saved["summary"]
        assert writes(demo_server, href) == []
        history.decide_approval(saved["approval_id"], "approve", "cli")
        executed = await runtime.call("submit", {**SAVE, "approval_id": saved["approval_id"]})
        assert executed["status"] == "ok", executed
        assert len(writes(demo_server, href)) == 1
        consumed = history.get_approval(saved["approval_id"], run_id)
        assert consumed is not None and consumed.execution_correlation_id is not None

    run_scenario(demo_project(make_auth_project), scenario)


def test_changed_value_invalidates_and_creates_new_request(
    make_auth_project: Callable[..., Any], demo_server: Any
) -> None:
    async def scenario(runtime: BrowserTools, history: HistoryStore, run_id: str, href: str):
        first = await runtime.call("fill", EMAIL)
        history.decide_approval(first["approval_id"], "approve", "cli")

        changed = await runtime.call(
            "fill", {**EMAIL, "value": "other@example.com", "approval_id": first["approval_id"]}
        )

        assert changed["decision"] == "awaiting_approval"
        assert changed["message"] == (
            "The action changed after approval (values); approval required again."
        )
        assert changed["changed_field"] == "values"
        assert changed["previous_approval_id"] == first["approval_id"]
        assert changed["approval_id"] != first["approval_id"]
        old = history.get_approval(first["approval_id"], run_id)
        assert old is not None and old.status == ApprovalStatus.INVALIDATED
        page = cast(Page, runtime.session.page)
        assert await page.get_by_label("E-mail", exact=True).input_value() != "other@example.com"
        assert writes(demo_server, href) == []

    run_scenario(demo_project(make_auth_project), scenario)


def test_expired_request_becomes_skip_no_response(
    make_auth_project: Callable[..., Any], demo_server: Any
) -> None:
    now = [datetime.now(UTC)]

    async def scenario(runtime: BrowserTools, history: HistoryStore, run_id: str, href: str):
        first = await runtime.call("fill", EMAIL)
        now[0] += timedelta(minutes=15)

        status = await runtime.call(
            "approval_status", {"approval_id": first["approval_id"], "wait_seconds": 0}
        )
        assert (status["status"], status["decision"], status["reason"]) == (
            "expired",
            "skip",
            "no_response",
        )
        assert history.get_run(run_id).status == RunStatus.RUNNING
        late = await runtime.call("fill", {**EMAIL, "approval_id": first["approval_id"]})
        assert (late["decision"], late["approval_status"]) == ("not_executed", "expired")
        observed = await runtime.call("observe", {})
        assert "content" in observed
        decided = [
            event.payload
            for event in history.list_events(run_id)
            if event.event_type == EventType.APPROVAL_DECIDED
        ]
        assert decided and isinstance(decided[0], dict) and decided[0]["status"] == "expired"

    run_scenario(demo_project(make_auth_project), scenario, clock=lambda: now[0])


def test_agent_end_run_while_waiting_skips_pending_request(
    make_auth_project: Callable[..., Any], demo_server: Any
) -> None:
    async def scenario(runtime: BrowserTools, history: HistoryStore, run_id: str, href: str):
        first = await runtime.call("fill", EMAIL)

        ended = await runtime.call("end_run", {"outcome": "blocked", "reason": "waiting"})

        assert ended["status"] == "blocked"
        record = history.get_approval(first["approval_id"], run_id)
        assert record is not None
        assert (record.status, record.decided_by) == (ApprovalStatus.SKIPPED, "system")
        assert writes(demo_server, href) == []

    run_scenario(demo_project(make_auth_project), scenario)


class Unavailable:
    channel = NotificationChannel.DESKTOP

    def __init__(self) -> None:
        self.messages: list[NotificationMessage] = []

    async def send(self, message: NotificationMessage) -> SendResult:
        self.messages.append(message)
        return SendResult(NotificationStatus.UNSUPPORTED, "notifier_not_found")


def test_request_notifies_or_records_degraded_fallback(
    make_auth_project: Callable[..., Any], demo_server: Any
) -> None:
    desktop = Unavailable()

    async def scenario(runtime: BrowserTools, history: HistoryStore, run_id: str, href: str):
        first = await runtime.call("fill", EMAIL)

        (message,) = desktop.messages
        assert message.title == "Lastro needs approval"
        assert message.body == first["summary"]
        assert message.command == first["command"]
        assert "qa@example.com" not in message.text
        degraded = [
            event.payload
            for event in history.list_events(run_id)
            if event.event_type == EventType.NOTIFICATION_DEGRADED
        ]
        assert len(degraded) == 1
        record = history.get_approval(first["approval_id"], run_id)
        assert record is not None and record.status == ApprovalStatus.PENDING
        (attempt,) = history.list_notifications(record)
        assert attempt.latency_ms is not None and attempt.latency_ms < 5000

    run_scenario(demo_project(make_auth_project), scenario, adapters=[desktop])


def test_end_from_gate_cancels_locks_and_closes_browser(
    make_auth_project: Callable[..., Any], demo_server: Any, runner: Any
) -> None:
    from lastro.cli.app import app

    async def scenario(runtime: BrowserTools, history: HistoryStore, run_id: str, href: str):
        first = await runtime.call("fill", EMAIL)

        ended = runner.invoke(app, ["approve", first["approval_id"], "--end"])

        assert ended.exit_code == 0, ended.output
        status = await runtime.call(
            "approval_status", {"approval_id": first["approval_id"], "wait_seconds": 0}
        )
        assert (status["status"], status["decided_by"]) == ("ended", "cli")
        assert "run_ended" in runtime.session.policy.locks
        assert runtime.session.page is None
        retry = await runtime.call("submit", SAVE)
        assert retry == {"error": "No active run. Start one with 'lastro run start'."}
        assert history.get_run(run_id).status == RunStatus.CANCELLED
        assert [item.kind.value for item in history.list_report_snapshots(run_id)] == ["partial"]
        assert writes(demo_server, href) == []

    run_scenario(demo_project(make_auth_project), scenario)
