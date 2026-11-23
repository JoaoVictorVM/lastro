"""Adapter de cron (Ubuntu): edita só um bloco delimitado do Lastro na crontab do usuário.

Linhas fora do bloco são preservadas byte a byte e nunca interpretadas. O conteúdo novo vai para
um arquivo privado e é instalado com `crontab <arquivo>`, sem passar por shell.
"""

import os
import shlex
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lastro.autonomous.models import (
    SCHEDULER_UNAVAILABLE,
    AutonomousError,
    InstallState,
    ScheduleDefinition,
)

BACKEND = "cron"
BEGIN = "# >>> lastro managed schedules (do not edit) >>>"
END = "# <<< lastro managed schedules <<<"
TIMEOUT_SECONDS = 30

type Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
        stdin=subprocess.DEVNULL,
    )


def marker(schedule_id: str) -> str:
    return f"# lastro:{schedule_id}"


def entry(schedule_id: str, command: list[str]) -> str:
    """Cada argumento é citado; `%` já foi recusado antes porque o cron o trata como newline."""
    quoted = " ".join(shlex.quote(argument) for argument in command)
    return f"* * * * * {quoted} >/dev/null 2>&1 {marker(schedule_id)}"


def split_block(text: str) -> tuple[list[str], list[str], list[str]]:
    """Antes, dentro e depois do bloco gerenciado; sem bloco, tudo é conteúdo do usuário."""
    lines = text.splitlines()
    if BEGIN not in lines or END not in lines:
        return lines, [], []
    start = lines.index(BEGIN)
    end = lines.index(END, start)
    return lines[:start], lines[start + 1 : end], lines[end + 1 :]


def render(before: list[str], managed: list[str], after: list[str]) -> str:
    lines = list(before)
    if managed:
        lines += [BEGIN, *sorted(managed), END]
    lines += after
    return "\n".join(lines) + "\n" if lines else ""


@dataclass
class CronScheduler:
    name: str = BACKEND
    run: Runner = _run
    workdir: Path | None = None

    def _call(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return self.run(arguments)
        except (OSError, subprocess.SubprocessError) as exc:
            raise AutonomousError(SCHEDULER_UNAVAILABLE, f"crontab unavailable: {exc}") from None

    def read(self) -> str:
        result = self._call(["crontab", "-l"])
        if result.returncode == 0:
            return result.stdout
        if "no crontab" in (result.stderr or "").lower():
            return ""
        raise AutonomousError(SCHEDULER_UNAVAILABLE, "crontab could not be read")

    def write(self, text: str) -> None:
        handle, raw = tempfile.mkstemp(prefix="lastro-cron-", suffix=".txt", dir=self.workdir)
        path = Path(raw)
        try:
            os.chmod(path, 0o600)
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
            result = self._call(["crontab", str(path)])
        finally:
            path.unlink(missing_ok=True)
        if result.returncode != 0:
            raise AutonomousError(SCHEDULER_UNAVAILABLE, "crontab refused the new table")

    def install(self, definition: ScheduleDefinition, command: list[str]) -> str:
        before, managed, after = split_block(self.read())
        tag = marker(definition.id)
        kept = [line for line in managed if not line.endswith(tag)]
        self.write(render(before, [*kept, entry(definition.id, command)], after))
        return tag

    def inspect(self, definition: ScheduleDefinition, command: list[str]) -> InstallState:
        _, managed, _ = split_block(self.read())
        tag = marker(definition.id)
        found = [line for line in managed if line.endswith(tag)]
        if not found:
            return InstallState.MISSING
        line = found[0]
        if line.lstrip().startswith("#"):
            return InstallState.DISABLED
        return (
            InstallState.INSTALLED
            if line == entry(definition.id, command)
            else (InstallState.DRIFTED)
        )

    def remove(self, definition: ScheduleDefinition) -> None:
        before, managed, after = split_block(self.read())
        tag = marker(definition.id)
        kept = [line for line in managed if not line.endswith(tag)]
        if kept == managed:
            return
        self.write(render(before, kept, after))
