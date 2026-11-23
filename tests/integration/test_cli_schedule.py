import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from lastro.autonomous.models import InstallState, ScheduleDefinition
from lastro.cli import schedule_cmd
from lastro.cli.app import app
from lastro.config.loader import ProjectContext
from lastro.history import open_history


class FakeBackend:
    name = "fake-scheduler"

    def __init__(self) -> None:
        self.installed: dict[str, list[str]] = {}
        self.removed: list[str] = []

    def install(self, definition: ScheduleDefinition, command: list[str]) -> str:
        self.installed[definition.id] = command
        return f"fake:{definition.id}"

    def inspect(self, definition: ScheduleDefinition, command: list[str]) -> InstallState:
        installed = self.installed.get(definition.id)
        if installed is None:
            return InstallState.MISSING
        return InstallState.INSTALLED if installed == command else InstallState.DRIFTED

    def remove(self, definition: ScheduleDefinition) -> None:
        self.removed.append(definition.id)
        self.installed.pop(definition.id, None)


@pytest.fixture
def backend(
    history_context: ProjectContext, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> FakeBackend:
    (workdir / "missions").mkdir()
    (workdir / "missions" / "daily.md").write_text("Explore employees", encoding="utf-8")
    fake = FakeBackend()
    monkeypatch.setattr(schedule_cmd, "backend_factory", lambda: fake)
    return fake


def _invoke(*arguments: str) -> Any:
    return CliRunner().invoke(app, list(arguments), catch_exceptions=False)


def _add(*extra: str, daily: str = "07:00") -> Any:
    return _invoke(
        "schedule",
        "add",
        "--name",
        "homolog-daily",
        "--daily",
        daily,
        "--mission",
        "missions/daily.md",
        "--profile",
        "homolog",
        "--json",
        *extra,
    )


def test_add_list_show_remove_json_contracts(backend: FakeBackend) -> None:
    added = _add()
    assert added.exit_code == 0, added.output
    data = json.loads(added.stdout)
    assert data["status"] == "installed"
    assert data["schedule_id"].startswith("asch_")
    assert (data["name"], data["profile"], data["daily"]) == ("homolog-daily", "homolog", "07:00")
    assert data["timezone"] == "America/Sao_Paulo"
    assert data["backend"] == "fake-scheduler" and data["drift"] is False
    assert data["next_due_at"].endswith("Z")
    command = backend.installed[data["schedule_id"]]
    assert command[-3:] == ["schedule", "tick", data["schedule_id"]]
    assert "Explore employees" not in " ".join(command)

    listed = json.loads(_invoke("schedule", "list", "--json").stdout)
    assert [item["schedule_id"] for item in listed["schedules"]] == [data["schedule_id"]]

    backend.installed[data["schedule_id"]] = [*command, "--tampered"]
    shown = json.loads(_invoke("schedule", "show", "homolog-daily", "--json").stdout)
    assert shown["install_state"] == "drifted" and shown["drift"] is True
    assert backend.installed[data["schedule_id"]][-1] == "--tampered"

    removed = _invoke("schedule", "remove", "homolog-daily", "--json")
    assert json.loads(removed.stdout)["status"] == "removed"
    assert backend.removed == [data["schedule_id"]]
    gone = _invoke("schedule", "show", "homolog-daily", "--json")
    assert gone.exit_code == 2
    assert json.loads(gone.stdout)["code"] == "invalid_autonomous_input"


def test_add_is_idempotent(backend: FakeBackend) -> None:
    first = json.loads(_add().stdout)
    second = _add()
    assert second.exit_code == 0
    assert json.loads(second.stdout)["schedule_id"] == first["schedule_id"]
    assert len(backend.installed) == 1


def test_replace_required(backend: FakeBackend, history_context: ProjectContext) -> None:
    first = json.loads(_add().stdout)
    conflict = _add(daily="08:00")
    assert conflict.exit_code == 2
    assert json.loads(conflict.stdout)["code"] == "schedule_conflict"
    store = open_history(history_context)
    current = store.autonomous.find_schedule("homolog-daily")
    assert current is not None and current.daily_time == "07:00"
    replaced = _add("--replace", daily="08:00")
    assert replaced.exit_code == 0
    assert json.loads(replaced.stdout)["schedule_id"] == first["schedule_id"]
    updated = store.autonomous.find_schedule("homolog-daily")
    assert updated is not None and updated.daily_time == "08:00"
    unsafe = _invoke(
        "schedule",
        "add",
        "--name",
        "outside",
        "--daily",
        "07:00",
        "--mission",
        "../outside.md",
        "--json",
    )
    assert unsafe.exit_code == 2


def test_tick_queues_once_per_local_date(
    backend: FakeBackend, history_context: ProjectContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    local = (now + timedelta(minutes=2)).astimezone(ZoneInfo("America/Sao_Paulo"))
    schedule_id = json.loads(_add(daily=local.strftime("%H:%M")).stdout)["schedule_id"]
    later = now + timedelta(minutes=5)
    monkeypatch.setattr(schedule_cmd, "utc_now", lambda: later)
    drained: list[str] = []
    monkeypatch.setattr(
        schedule_cmd, "drain", lambda loaded, store, profile: drained.append(profile)
    )

    first = _invoke("schedule", "tick", schedule_id, "--json")
    second = _invoke("schedule", "tick", schedule_id, "--json")
    assert json.loads(first.stdout)["status"] == "queued", first.output
    assert json.loads(second.stdout)["status"] == "duplicate"
    items = open_history(history_context).autonomous.list_items()
    assert len(items) == 1
    assert items[0].trigger_kind.value == "schedule"
    assert items[0].mission.text == "Explore employees"
    assert drained == ["homolog", "homolog"]
