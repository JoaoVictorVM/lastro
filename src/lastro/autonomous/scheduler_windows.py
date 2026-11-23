"""Adapter do Windows Task Scheduler via `schtasks`, com argumentos separados.

A tarefa acorda o tick a cada minuto; a regra de fuso, DST, atraso e idempotência é do domínio.
A identidade externa deriva do schedule id. Alteração feita fora do Lastro aparece como drift e
nunca é sobrescrita implicitamente.
"""

import locale
import subprocess
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable
from dataclasses import dataclass

from lastro.autonomous.models import (
    SCHEDULER_UNAVAILABLE,
    AutonomousError,
    InstallState,
    ScheduleDefinition,
)

BACKEND = "windows-task-scheduler"
FOLDER = "\\Lastro\\"
TIMEOUT_SECONDS = 30
_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}

type Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _decode(data: bytes) -> str:
    """`schtasks /XML` escreve UTF-16; as demais saídas usam a página de código do console."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in data[:200]:
        return data.decode("utf-16", errors="replace")
    return data.decode(locale.getpreferredencoding(False), errors="replace")


def _run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        arguments,
        capture_output=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    return subprocess.CompletedProcess(
        arguments, completed.returncode, _decode(completed.stdout), _decode(completed.stderr)
    )


def task_name(schedule_id: str) -> str:
    return f"{FOLDER}lastro-{schedule_id}"


@dataclass
class WindowsScheduler:
    name: str = BACKEND
    run: Runner = _run

    def _call(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return self.run(arguments)
        except (OSError, subprocess.SubprocessError) as exc:
            raise AutonomousError(SCHEDULER_UNAVAILABLE, f"schtasks unavailable: {exc}") from None

    def install(self, definition: ScheduleDefinition, command: list[str]) -> str:
        name = task_name(definition.id)
        action = subprocess.list2cmdline(command)
        result = self._call(
            [
                "schtasks",
                "/Create",
                "/F",
                "/TN",
                name,
                "/SC",
                "MINUTE",
                "/MO",
                "1",
                "/TR",
                action,
            ]
        )
        if result.returncode != 0:
            raise AutonomousError(
                SCHEDULER_UNAVAILABLE, "Task Scheduler refused the task (permission or policy)"
            )
        return name

    def inspect(self, definition: ScheduleDefinition, command: list[str]) -> InstallState:
        name = definition.external_id or task_name(definition.id)
        result = self._call(["schtasks", "/Query", "/TN", name, "/XML"])
        if result.returncode != 0:
            return InstallState.MISSING
        try:
            root = ElementTree.fromstring(result.stdout.strip())
        except ElementTree.ParseError:
            return InstallState.DRIFTED
        enabled = root.findtext("t:Settings/t:Enabled", default="true", namespaces=_NS)
        executable = root.findtext("t:Actions/t:Exec/t:Command", default="", namespaces=_NS)
        arguments = root.findtext("t:Actions/t:Exec/t:Arguments", default="", namespaces=_NS)
        interval = root.findtext("t:Triggers/*/t:Repetition/t:Interval", default="", namespaces=_NS)
        expected_arguments = subprocess.list2cmdline(command[1:])
        same = (
            executable.strip().strip('"').lower() == command[0].lower()
            and arguments.strip() == expected_arguments
            and interval in ("", "PT1M")
        )
        if not same:
            return InstallState.DRIFTED
        if enabled.strip().lower() == "false":
            return InstallState.DISABLED
        return InstallState.INSTALLED

    def remove(self, definition: ScheduleDefinition) -> None:
        name = definition.external_id or task_name(definition.id)
        query = self._call(["schtasks", "/Query", "/TN", name])
        if query.returncode != 0:
            return
        result = self._call(["schtasks", "/Delete", "/F", "/TN", name])
        if result.returncode != 0:
            raise AutonomousError(
                SCHEDULER_UNAVAILABLE, "Task Scheduler refused to remove the task"
            )
