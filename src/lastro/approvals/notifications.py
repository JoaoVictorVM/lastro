"""Notificações de pedidos: best-effort, com prazo de 5 segundos e degradação auditada.

Nenhum resultado de notificação altera o pedido. Sem entrega, o fallback é o banner com sino
do `lastro tail`, e o histórico registra `notification_degraded`.
"""

import asyncio
import contextlib
import os
import shutil
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from lastro.approvals.models import NOTIFICATION_TITLE, approve_command
from lastro.history import (
    ApprovalRecord,
    HistoryStore,
    NotificationChannel,
    NotificationStatus,
)
from lastro.timeutil import utc_now

DEADLINE_SECONDS = 5.0
DISABLE_VARIABLE = "LASTRO_DESKTOP_NOTIFICATIONS"
# Toast nativo pelo AppUserModelID do PowerShell; título e corpo chegam por variável de ambiente,
# nunca interpolados no script.
WINDOWS_TOAST = (
    "$ErrorActionPreference='Stop';"
    "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,"
    "ContentType=WindowsRuntime]>$null;"
    "$t=[Windows.UI.Notifications.ToastTemplateType]::ToastText02;"
    "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($t);"
    "$n=$x.GetElementsByTagName('text');"
    "$n.Item(0).AppendChild($x.CreateTextNode($env:LASTRO_NOTIFY_TITLE))>$null;"
    "$n.Item(1).AppendChild($x.CreateTextNode($env:LASTRO_NOTIFY_BODY))>$null;"
    "$a='{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($a)"
    ".Show([Windows.UI.Notifications.ToastNotification]::new($x))"
)
MAC_SCRIPT = (
    "on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run"
)


@dataclass(frozen=True)
class NotificationMessage:
    approval_id: str
    title: str
    body: str
    command: str

    @classmethod
    def from_record(cls, record: ApprovalRecord) -> "NotificationMessage":
        command = approve_command(record.action_id)
        return cls(record.action_id, NOTIFICATION_TITLE, record.summary, command)

    @property
    def text(self) -> str:
        return f"{self.body}\n{self.command}"


@dataclass(frozen=True)
class SendResult:
    status: NotificationStatus
    error_code: str | None = None


class NotificationAdapter(Protocol):
    channel: NotificationChannel

    async def send(self, message: NotificationMessage) -> SendResult: ...


def desktop_command(
    message: NotificationMessage,
    platform: str = sys.platform,
    which: Callable[[str], str | None] = shutil.which,
) -> tuple[list[str], dict[str, str]] | None:
    """Comando do notificador nativo, com argumentos separados e sem shell."""
    if platform == "win32":
        executable = which("powershell")
        if executable is None:
            return None
        env = {"LASTRO_NOTIFY_TITLE": message.title, "LASTRO_NOTIFY_BODY": message.text}
        return [executable, "-NoProfile", "-NonInteractive", "-Command", WINDOWS_TOAST], env
    if platform == "darwin":
        executable = which("osascript")
        if executable is None:
            return None
        return [executable, "-e", MAC_SCRIPT, message.title, message.text], {}
    executable = which("notify-send")
    if executable is None:
        return None
    return [executable, "--app-name=Lastro", message.title, message.text], {}


class DesktopNotifier:
    channel = NotificationChannel.DESKTOP

    def __init__(
        self,
        platform: str = sys.platform,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self.platform = platform
        self.which = which

    async def send(self, message: NotificationMessage) -> SendResult:
        if os.environ.get(DISABLE_VARIABLE, "").strip().lower() in {"0", "off", "false", "no"}:
            return SendResult(NotificationStatus.UNSUPPORTED, "desktop_disabled")
        command = desktop_command(message, self.platform, self.which)
        if command is None:
            return SendResult(NotificationStatus.UNSUPPORTED, "notifier_not_found")
        arguments, extra = command
        process = await asyncio.create_subprocess_exec(
            *arguments,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, **extra},
        )
        try:
            code = await process.wait()
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            raise
        if code != 0:
            return SendResult(NotificationStatus.DEGRADED, f"exit_{code}")
        return SendResult(NotificationStatus.DELIVERED)


class NotificationRouter:
    """Tenta cada adapter dentro do prazo comum e registra tentativa, latência e degradação."""

    def __init__(
        self,
        history: HistoryStore,
        adapters: Sequence[NotificationAdapter],
        *,
        deadline: float = DEADLINE_SECONDS,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        attempt_key: str = "initial",
    ) -> None:
        self.history = history
        self.adapters = tuple(adapters)
        self.deadline = deadline
        self.clock = clock
        self.monotonic = monotonic
        self.attempt_key = attempt_key

    async def notify(self, record: ApprovalRecord) -> list[SendResult]:
        message = NotificationMessage.from_record(record)
        started = self.monotonic()
        results: list[SendResult] = []
        for adapter in self.adapters:
            requested = self.clock()
            remaining = max(self.deadline - (self.monotonic() - started), 0.0)
            began = self.monotonic()
            try:
                result = await asyncio.wait_for(adapter.send(message), remaining)
            except TimeoutError:
                result = SendResult(NotificationStatus.DEGRADED, "timeout")
            except Exception as error:
                result = SendResult(NotificationStatus.DEGRADED, type(error).__name__)
            latency = int((self.monotonic() - began) * 1000)
            with contextlib.suppress(Exception):
                self.history.record_notification(
                    record,
                    adapter.channel,
                    result.status,
                    requested_at=requested,
                    latency_ms=latency,
                    error_code=result.error_code,
                    attempt_key=self.attempt_key,
                )
            results.append(result)
        return results


def default_router(history: HistoryStore) -> NotificationRouter:
    return NotificationRouter(history, [DesktopNotifier()])
