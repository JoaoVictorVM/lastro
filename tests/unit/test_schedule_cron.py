import shlex
import subprocess
from pathlib import Path

import pytest

from lastro.autonomous.models import (
    AutonomousError,
    InstallState,
    ScheduleDefinition,
    ScheduleState,
)
from lastro.autonomous.scheduler import tick_command
from lastro.autonomous.scheduler_cron import BEGIN, END, CronScheduler

FIRST = "asch_" + "a" * 32
SECOND = "asch_" + "b" * 32
PYTHON = Path("/usr/bin/python3")
USER_TABLE = "MAILTO=dev@example.com\n0 3 * * * /usr/local/bin/backup.sh # nightly\n"


def _definition(schedule_id: str) -> ScheduleDefinition:
    return ScheduleDefinition(
        id=schedule_id,
        project_id="p",
        name=schedule_id[-4:],
        profile="homolog",
        mission_path="missions/daily.md",
        daily_time="07:00",
        timezone="America/Sao_Paulo",
        executor="claude-code",
        settings={},
        fingerprint="f" * 64,
        backend="cron",
        external_id=None,
        state=ScheduleState.ACTIVE,
        created_at="",
        updated_at="",
        removed_at=None,
    )


class FakeCrontab:
    def __init__(self, table: str) -> None:
        self.table = table
        self.calls: list[list[str]] = []

    def __call__(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(arguments)
        if arguments == ["crontab", "-l"]:
            if not self.table:
                return subprocess.CompletedProcess(arguments, 1, "", "no crontab for user")
            return subprocess.CompletedProcess(arguments, 0, self.table, "")
        assert arguments[0] == "crontab" and len(arguments) == 2
        self.table = Path(arguments[1]).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(arguments, 0, "", "")


def _command(tmp_path: Path, schedule_id: str) -> list[str]:
    return tick_command(tmp_path / "lastro.toml", schedule_id, python=PYTHON)


def test_managed_block_is_stable(tmp_path: Path) -> None:
    fake = FakeCrontab("")
    cron = CronScheduler(run=fake, workdir=tmp_path)
    cron.install(_definition(FIRST), _command(tmp_path, FIRST))
    cron.install(_definition(SECOND), _command(tmp_path, SECOND))
    snapshot = fake.table
    cron.install(_definition(FIRST), _command(tmp_path, FIRST))
    assert fake.table == snapshot
    assert snapshot.count(BEGIN) == 1 and snapshot.count(END) == 1
    assert cron.inspect(_definition(FIRST), _command(tmp_path, FIRST)) == InstallState.INSTALLED
    cron.remove(_definition(FIRST))
    assert cron.inspect(_definition(FIRST), _command(tmp_path, FIRST)) == InstallState.MISSING
    cron.remove(_definition(SECOND))
    assert BEGIN not in fake.table
    assert not list(tmp_path.glob("lastro-cron-*"))


def test_unmanaged_entries_preserved(tmp_path: Path) -> None:
    fake = FakeCrontab(USER_TABLE)
    cron = CronScheduler(run=fake, workdir=tmp_path)
    cron.install(_definition(FIRST), _command(tmp_path, FIRST))
    assert fake.table.startswith(USER_TABLE)
    lines = fake.table.splitlines()
    managed = lines[lines.index(BEGIN) + 1 : lines.index(END)]
    managed[0] = managed[0].replace("tick", "tick --extra")
    fake.table = "\n".join([*lines[: lines.index(BEGIN) + 1], *managed, END]) + "\n"
    assert cron.inspect(_definition(FIRST), _command(tmp_path, FIRST)) == InstallState.DRIFTED
    cron.remove(_definition(FIRST))
    assert fake.table == USER_TABLE


def test_shell_metacharacters_never_enter_command(tmp_path: Path) -> None:
    hostile = tmp_path / "a;rm -rf ~ $(whoami)` x"
    hostile.mkdir()
    fake = FakeCrontab("")
    cron = CronScheduler(run=fake, workdir=tmp_path)
    command = _command(hostile, FIRST)
    cron.install(_definition(FIRST), command)
    line = next(item for item in fake.table.splitlines() if item.startswith("* * * * *"))
    shell_part = line.removeprefix("* * * * * ").split(" >/dev/null")[0]
    assert shlex.split(shell_part) == command
    with pytest.raises(AutonomousError):
        _command(tmp_path / "100%", FIRST)
