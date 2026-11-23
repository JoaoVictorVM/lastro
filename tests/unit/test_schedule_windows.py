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
from lastro.autonomous.scheduler_windows import WindowsScheduler, task_name

SCHEDULE_ID = "asch_" + "a" * 32
PYTHON = Path("C:/Program Files/Python312/pythonw.exe")
XML = (
    '<?xml version="1.0" encoding="UTF-16"?>'
    '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
    "<Triggers><TimeTrigger><Repetition><Interval>PT1M</Interval></Repetition>"
    "</TimeTrigger></Triggers>"
    "<Settings><Enabled>{enabled}</Enabled></Settings>"
    '<Actions Context="Author"><Exec><Command>{command}</Command>'
    "<Arguments>{arguments}</Arguments></Exec></Actions>"
    "</Task>"
)


def _definition() -> ScheduleDefinition:
    return ScheduleDefinition(
        id=SCHEDULE_ID,
        project_id="p",
        name="daily",
        profile="homolog",
        mission_path="missions/daily.md",
        daily_time="07:00",
        timezone="America/Sao_Paulo",
        executor="claude-code",
        settings={},
        fingerprint="f" * 64,
        backend="windows-task-scheduler",
        external_id=task_name(SCHEDULE_ID),
        state=ScheduleState.ACTIVE,
        created_at="",
        updated_at="",
        removed_at=None,
    )


class FakeSchtasks:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.stdout = stdout
        self.returncode = returncode

    def __call__(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        assert isinstance(arguments, list)
        self.calls.append(arguments)
        return subprocess.CompletedProcess(arguments, self.returncode, self.stdout, "")


def test_arguments_are_separate_and_secret_free(tmp_path: Path) -> None:
    config = tmp_path / "lastro.toml"
    command = tick_command(config, SCHEDULE_ID, python=PYTHON)
    assert command[1:4] == ["-m", "lastro", "--config"]
    assert command[-3:] == ["schedule", "tick", SCHEDULE_ID]
    fake = FakeSchtasks()
    name = WindowsScheduler(run=fake).install(_definition(), command)
    created = fake.calls[0]
    assert created[:2] == ["schtasks", "/Create"]
    assert created[created.index("/TN") + 1] == name == "\\Lastro\\lastro-" + SCHEDULE_ID
    action = created[created.index("/TR") + 1]
    assert action == subprocess.list2cmdline(command)
    for forbidden in ("mission", "webhook", "password", "Explore"):
        assert forbidden not in action.lower().replace("missions", "")
    with pytest.raises(AutonomousError):
        tick_command(tmp_path / 'with"quote' / "lastro.toml", SCHEDULE_ID, python=PYTHON)
    with pytest.raises(AutonomousError):
        tick_command(config, "asch_../../evil", python=PYTHON)


def test_manifest_drift_detected(tmp_path: Path) -> None:
    command = tick_command(tmp_path / "lastro.toml", SCHEDULE_ID, python=PYTHON)
    arguments = subprocess.list2cmdline(command[1:])
    ok = XML.format(enabled="true", command=str(PYTHON), arguments=arguments)
    assert WindowsScheduler(run=FakeSchtasks(ok)).inspect(_definition(), command) == (
        InstallState.INSTALLED
    )
    edited = XML.format(enabled="true", command=str(PYTHON), arguments=arguments + " --extra")
    assert WindowsScheduler(run=FakeSchtasks(edited)).inspect(_definition(), command) == (
        InstallState.DRIFTED
    )
    disabled = XML.format(enabled="false", command=str(PYTHON), arguments=arguments)
    assert WindowsScheduler(run=FakeSchtasks(disabled)).inspect(_definition(), command) == (
        InstallState.DISABLED
    )
    missing = FakeSchtasks(returncode=1)
    assert WindowsScheduler(run=missing).inspect(_definition(), command) == InstallState.MISSING
    # Inspecionar nunca reinstala nem altera a tarefa.
    assert all(call[1] == "/Query" for call in missing.calls)


def test_replace_is_explicit(tmp_path: Path) -> None:
    fake = FakeSchtasks(returncode=1)
    with pytest.raises(AutonomousError) as refused:
        WindowsScheduler(run=fake).install(
            _definition(), tick_command(tmp_path / "lastro.toml", SCHEDULE_ID, python=PYTHON)
        )
    assert refused.value.code == "scheduler_unavailable"
    absent = FakeSchtasks(returncode=1)
    WindowsScheduler(run=absent).remove(_definition())
    assert [call[1] for call in absent.calls] == ["/Query"]
