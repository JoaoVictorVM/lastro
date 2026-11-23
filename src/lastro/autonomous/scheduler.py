"""Regra temporal da agenda diária e tick idempotente.

O agendador do SO só acorda o Lastro; a decisão de vencimento é daqui, no fuso do projeto. A
chave idempotente é agenda + data local. Horário ambíguo (fim do horário de verão) executa uma
vez, no primeiro instante; horário inexistente (início) executa no primeiro instante local
válido. Atraso de até 60 minutos recupera uma vez; atraso maior vira `missed`.
"""

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from lastro.autonomous.models import (
    INVALID_INPUT,
    LATE_WINDOW_MINUTES,
    MISSION_SNAPSHOT_INVALID,
    AutonomousError,
    EnqueueResult,
    InstallState,
    MissionSnapshot,
    Occurrence,
    OccurrenceStatus,
    ScheduleDefinition,
    ScheduleState,
    TriggerInput,
    TriggerKind,
    canonical,
)
from lastro.history.autonomous import AutonomousRepository
from lastro.history.database import db_timestamp
from lastro.timeutil import parse_timestamp

DAILY = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")
SCHEDULE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_MISSION_BYTES = 256 * 1024
MISSED_REASON = "late_beyond_window"


def parse_daily(value: str) -> time:
    match = DAILY.fullmatch(value)
    if match is None:
        raise AutonomousError(INVALID_INPUT, "--daily must be HH:MM in 24-hour time")
    return time(int(match.group(1)), int(match.group(2)))


def validate_name(value: str) -> str:
    if not SCHEDULE_NAME.fullmatch(value):
        raise AutonomousError(INVALID_INPUT, "schedule name must match [a-z0-9][a-z0-9-]{0,63}")
    return value


def due_instant(local_date: date, at: time, zone: ZoneInfo) -> datetime:
    """Instante UTC em que `at` ocorre na data local, com a regra explícita de DST."""
    naive = datetime.combine(local_date, at)
    first = naive.replace(tzinfo=zone, fold=0)
    # Ida e volta por UTC revela horário inexistente: o relógio local não retorna ao mesmo valor.
    roundtrip = first.astimezone(UTC).astimezone(zone)
    if roundtrip.replace(tzinfo=None) != naive:
        return _first_valid_after(naive, zone)
    return first.astimezone(UTC)


def _first_valid_after(naive: datetime, zone: ZoneInfo) -> datetime:
    """Primeiro instante local válido depois do salto, em passos de minuto."""
    candidate = naive
    for _ in range(24 * 60):
        candidate += timedelta(minutes=1)
        aware = candidate.replace(tzinfo=zone, fold=0)
        if aware.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == candidate:
            return aware.astimezone(UTC)
    raise AutonomousError(INVALID_INPUT, "no valid local instant after the configured time")


@dataclass(frozen=True)
class TickDecision:
    status: OccurrenceStatus
    local_date: str | None
    due_at: datetime | None


def decide(
    daily: str, timezone: str, now: datetime, *, not_before: datetime | None = None
) -> TickDecision:
    """Ocorrência mais recente já vencida (hoje ou ontem) e se ainda cabe na janela."""
    zone = ZoneInfo(timezone)
    at = parse_daily(daily)
    today = now.astimezone(zone).date()
    for local in (today, today - timedelta(days=1)):
        due = due_instant(local, at, zone)
        if due > now:
            continue
        if not_before is not None and due < not_before:
            return TickDecision(OccurrenceStatus.NOT_DUE, None, None)
        if now - due <= timedelta(minutes=LATE_WINDOW_MINUTES):
            return TickDecision(OccurrenceStatus.QUEUED, local.isoformat(), due)
        return TickDecision(OccurrenceStatus.MISSED, local.isoformat(), due)
    return TickDecision(OccurrenceStatus.NOT_DUE, None, None)


def next_due(daily: str, timezone: str, now: datetime) -> datetime:
    zone = ZoneInfo(timezone)
    at = parse_daily(daily)
    local = now.astimezone(zone).date()
    for offset in range(3):
        due = due_instant(local + timedelta(days=offset), at, zone)
        if due > now:
            return due
    raise AutonomousError(INVALID_INPUT, "could not compute the next occurrence")


def safe_mission_path(project_dir: Path, value: str) -> str:
    """Arquivo regular sob a raiz do projeto, como caminho relativo POSIX."""
    root = project_dir.resolve()
    candidate = Path(value)
    target = (candidate if candidate.is_absolute() else root / candidate).resolve()
    try:
        relative = target.relative_to(root)
    except ValueError:
        raise AutonomousError(
            INVALID_INPUT, "mission must be a file under the project root"
        ) from None
    if not target.is_file():
        raise AutonomousError(INVALID_INPUT, f"mission file not found: {relative.as_posix()}")
    return relative.as_posix()


def freeze_mission_file(project_dir: Path, relative: str) -> MissionSnapshot:
    """Congela o conteúdo atual da missão antes de entrar na fila."""
    path = project_dir / safe_mission_path(project_dir, relative)
    data = path.read_bytes()
    if len(data) > MAX_MISSION_BYTES:
        raise AutonomousError(MISSION_SNAPSHOT_INVALID, "mission file is too large")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise AutonomousError(MISSION_SNAPSHOT_INVALID, "mission file must be UTF-8") from None
    return MissionSnapshot("file", path.relative_to(project_dir.resolve()).as_posix(), text)


def schedule_fingerprint(
    name: str, profile: str, mission_path: str, daily: str, timezone: str, executor: str
) -> str:
    data = {
        "name": name,
        "profile": profile,
        "mission_path": mission_path,
        "daily": daily,
        "timezone": timezone,
        "executor": executor,
    }
    return hashlib.sha256(canonical(data).encode("utf-8")).hexdigest()


class SchedulerBackend(Protocol):
    """Agendador do SO; o comando instalado só chama `lastro schedule tick <id>`."""

    name: str

    def install(self, definition: ScheduleDefinition, command: list[str]) -> str: ...

    def inspect(self, definition: ScheduleDefinition, command: list[str]) -> InstallState: ...

    def remove(self, definition: ScheduleDefinition) -> None: ...


@dataclass(frozen=True)
class TickOutcome:
    status: OccurrenceStatus
    schedule_id: str
    local_date: str | None
    occurrence: Occurrence | None = None
    enqueued: EnqueueResult | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "schedule_id": self.schedule_id,
            "local_date": self.local_date,
            "item_id": self.enqueued.item.id if self.enqueued else None,
            "reason": self.occurrence.reason if self.occurrence else None,
        }


def tick(
    repository: AutonomousRepository,
    schedule: ScheduleDefinition,
    project_dir: Path,
    now: datetime,
    enqueue: Callable[[TriggerInput], EnqueueResult],
) -> TickOutcome:
    """Uma ocorrência por data local; repetição é duplicata, atraso grande é missed."""
    if schedule.state in {ScheduleState.REMOVED, ScheduleState.DISABLED}:
        return TickOutcome(OccurrenceStatus.NOT_DUE, schedule.id, None)
    decision = decide(
        schedule.daily_time,
        schedule.timezone,
        now,
        not_before=parse_timestamp(schedule.updated_at),
    )
    if decision.local_date is None or decision.due_at is None:
        return TickOutcome(OccurrenceStatus.NOT_DUE, schedule.id, None)
    existing = repository.get_occurrence(schedule.id, decision.local_date)
    if existing is not None:
        return TickOutcome(OccurrenceStatus.DUPLICATE, schedule.id, decision.local_date, existing)
    due_at = db_timestamp(decision.due_at)
    if decision.status == OccurrenceStatus.MISSED:
        occurrence, _ = repository.record_occurrence(
            schedule.id, decision.local_date, due_at, OccurrenceStatus.MISSED, reason=MISSED_REASON
        )
        return TickOutcome(occurrence.status, schedule.id, decision.local_date, occurrence)
    mission = freeze_mission_file(project_dir, schedule.mission_path)
    trigger = TriggerInput(
        kind=TriggerKind.SCHEDULE,
        key=f"schedule:{schedule.id}:{decision.local_date}",
        profile=schedule.profile,
        mission=mission,
        payload={"local_date": decision.local_date, "due_at": due_at},
        schedule_id=schedule.id,
        executor=schedule.executor,
    )
    # A chave idempotente garante que uma queda entre os dois passos não duplica o item.
    result = enqueue(trigger)
    occurrence, created = repository.record_occurrence(
        schedule.id,
        decision.local_date,
        due_at,
        OccurrenceStatus.QUEUED,
        item_id=result.item.id,
    )
    status = OccurrenceStatus.QUEUED if created else OccurrenceStatus.DUPLICATE
    return TickOutcome(status, schedule.id, decision.local_date, occurrence, result)


def utc(moment: datetime) -> datetime:
    return moment.astimezone(UTC)


UNSAFE_COMMAND = re.compile(r"[\"%\r\n\x00]")


def tick_command(config_path: Path, schedule_id: str, python: Path | None = None) -> list[str]:
    """Argumentos separados do tick; nunca contém missão, webhook, usuário ou segredo.

    No Windows usa pythonw.exe quando disponível, para o tick não abrir janela de console.
    """
    import sys

    executable = python or Path(sys.executable)
    if sys.platform == "win32" and python is None:
        windowless = executable.with_name("pythonw.exe")
        if windowless.is_file():
            executable = windowless
    if not re.fullmatch(r"asch_[0-9a-f]{32}", schedule_id):
        raise AutonomousError(INVALID_INPUT, "invalid schedule id")
    arguments = [
        str(executable),
        "-m",
        "lastro",
        "--config",
        str(config_path.resolve()),
        "schedule",
        "tick",
        schedule_id,
    ]
    for argument in arguments:
        if UNSAFE_COMMAND.search(argument):
            raise AutonomousError(
                INVALID_INPUT, "paths with quotes, percent signs or line breaks cannot be scheduled"
            )
    return arguments


def command_fingerprint(command: list[str]) -> str:
    return hashlib.sha256(canonical(command).encode("utf-8")).hexdigest()


def default_backend() -> SchedulerBackend:
    """Task Scheduler no Windows, cron nos demais sistemas suportados."""
    import sys

    if sys.platform == "win32":
        from lastro.autonomous.scheduler_windows import WindowsScheduler

        return WindowsScheduler()
    from lastro.autonomous.scheduler_cron import CronScheduler

    return CronScheduler()
