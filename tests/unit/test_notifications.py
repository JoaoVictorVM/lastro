import asyncio
import time
from collections.abc import Callable
from datetime import datetime

import pytest

from lastro.approvals.notifications import (
    DesktopNotifier,
    NotificationMessage,
    NotificationRouter,
    SendResult,
    desktop_command,
)
from lastro.config.loader import ProjectContext
from lastro.history import (
    ApprovalRecord,
    EventType,
    HistoryStore,
    NotificationChannel,
    NotificationStatus,
    TextMission,
    open_history,
)

MESSAGE = NotificationMessage(
    "act1", "Lastro needs approval", "edit employee #57 (phone) in Acme", "lastro approve act1"
)


class Adapter:
    channel = NotificationChannel.DESKTOP

    def __init__(self, result: SendResult | None = None, delay: float = 0.0) -> None:
        self.result = result or SendResult(NotificationStatus.DELIVERED)
        self.delay = delay
        self.messages: list[NotificationMessage] = []

    async def send(self, message: NotificationMessage) -> SendResult:
        self.messages.append(message)
        await asyncio.sleep(self.delay)
        return self.result


class Broken(Adapter):
    channel = NotificationChannel.EXECUTOR_HOOK

    async def send(self, message: NotificationMessage) -> SendResult:
        raise OSError("dbus offline")


def pending(
    context: ProjectContext, clock: Callable[[], datetime]
) -> tuple[HistoryStore, ApprovalRecord]:
    history = open_history(context, clock=clock)
    run_id = history.create_run(context, TextMission("n")).record.id
    record = history.request_approval(
        run_id, "act1", "a" * 64, {"summary": "edit employee #57 (phone) in Acme"}
    )
    return history, record


def degraded(history: HistoryStore, record: ApprovalRecord) -> list[object]:
    return [
        event.payload
        for event in history.list_events(record.run_id)
        if event.event_type == EventType.NOTIFICATION_DEGRADED
    ]


def test_notification_within_five_seconds_or_degraded(
    history_context: ProjectContext, fake_clock: Callable[[], datetime]
) -> None:
    history, record = pending(history_context, fake_clock)
    fast = Adapter()
    started = time.monotonic()

    (result,) = asyncio.run(NotificationRouter(history, [fast]).notify(record))

    assert time.monotonic() - started < 5
    assert result.status == NotificationStatus.DELIVERED
    assert fast.messages[0].body == "edit employee #57 (phone) in Acme"
    assert fast.messages[0].command == "lastro approve act1"
    (attempt,) = history.list_notifications(record)
    assert attempt.status == NotificationStatus.DELIVERED
    assert attempt.latency_ms is not None and attempt.latency_ms < 5000
    assert degraded(history, record) == []


def test_slow_or_broken_notifier_degrades_without_changing_request(
    history_context: ProjectContext, fake_clock: Callable[[], datetime]
) -> None:
    history, record = pending(history_context, fake_clock)
    slow = Adapter(delay=5)
    started = time.monotonic()

    results = asyncio.run(
        NotificationRouter(history, [Broken(), slow], deadline=0.2, attempt_key="t").notify(record)
    )

    assert time.monotonic() - started < 2
    assert [item.error_code for item in results] == ["OSError", "timeout"]
    payloads = degraded(history, record)
    assert len(payloads) == 2
    assert isinstance(payloads[0], dict)
    assert payloads[0]["fallback"] == "tail_banner_and_bell"
    current = history.get_approval("act1")
    assert current is not None and current.status.value == "pending"


def test_desktop_commands_never_use_a_shell() -> None:
    def which(name: str) -> str:
        return "/bin/" + name

    windows = desktop_command(MESSAGE, "win32", which)
    mac = desktop_command(MESSAGE, "darwin", which)
    linux = desktop_command(MESSAGE, "linux", which)

    assert windows is not None and mac is not None and linux is not None
    assert MESSAGE.body not in " ".join(windows[0])
    assert windows[1]["LASTRO_NOTIFY_BODY"] == MESSAGE.text
    assert mac[0][-2:] == [MESSAGE.title, MESSAGE.text]
    assert linux[0][-2:] == [MESSAGE.title, MESSAGE.text]
    assert desktop_command(MESSAGE, "linux", lambda name: None) is None


def test_desktop_notifier_reports_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    missing = DesktopNotifier("linux", lambda name: None)
    assert asyncio.run(missing.send(MESSAGE)) == SendResult(
        NotificationStatus.UNSUPPORTED, "notifier_not_found"
    )
    monkeypatch.setenv("LASTRO_DESKTOP_NOTIFICATIONS", "off")
    disabled = DesktopNotifier("linux", lambda name: "/bin/" + name)
    assert asyncio.run(disabled.send(MESSAGE)).error_code == "desktop_disabled"
