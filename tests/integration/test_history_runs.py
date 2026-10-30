import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lastro.cli import run_cmd
from lastro.cli.app import app
from lastro.config.loader import ProjectContext
from lastro.history import (
    PROCESS_LOST_REASON,
    ActiveRunExistsError,
    CreatedRun,
    EventType,
    FileMission,
    InvalidMissionError,
    InvalidTransitionError,
    MissionSource,
    RunStatus,
    TextMission,
    open_history,
)

LOST_PID = 424242


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_run_exists_before_target_callback(
    runner: CliRunner, history_context: ProjectContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[str] = []

    def spy(created: CreatedRun) -> None:
        conn = _connect(history_context.project_data_dir / "history.sqlite")
        try:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (created.record.id,)).fetchone()
            events = conn.execute(
                "SELECT event_type, sequence FROM events WHERE run_id = ?", (created.record.id,)
            ).fetchall()
        finally:
            conn.close()
        assert row is not None and row["status"] == "running"
        assert [(event["event_type"], event["sequence"]) for event in events] == [
            ("run_started", 1)
        ]
        assert created.paths.run_dir.is_dir()
        assert created.paths.evidence_dir.is_dir()
        pointer = json.loads(
            (history_context.project_data_dir / "active-run.json").read_text(encoding="utf-8")
        )
        assert pointer["run_id"] == created.record.id
        observed.append(created.record.id)

    monkeypatch.setattr(run_cmd, "handoff", spy)

    result = runner.invoke(app, ["--profile", "homolog", "run", "start", "--mission", "Explore"])

    assert result.exit_code == 0, result.output
    assert len(observed) == 1
    assert f"Run: {observed[0]}" in result.stdout
    assert str(history_context.project_data_dir / "runs" / observed[0]) in result.stdout


def test_new_project_uses_stable_project_id_for_shared_history(
    history_context_for: Callable[[str], ProjectContext],
) -> None:
    homolog, local = history_context_for("homolog"), history_context_for("local")
    first = open_history(homolog)
    created = first.create_run(homolog, TextMission("Cadastro"))
    first.finish_run(created.record.id, RunStatus.COMPLETED)

    second = open_history(local)
    other = second.create_run(local, TextMission("Busca"))

    assert first.database_path == second.database_path
    assert second.database_path == (
        homolog.private_data_root / homolog.require_project_id() / "history.sqlite"
    )
    runs = {summary.record.id: summary.record for summary in second.list_runs()}
    assert runs[created.record.id].profile == "homolog"
    assert runs[other.record.id].profile == "local"
    assert {record.project_id for record in runs.values()} == {homolog.require_project_id()}
    assert runs[other.record.id].environment_url == "https://local.example.test"
    assert runs[other.record.id].tenant == "acme"


def test_run_id_uses_display_timezone_and_four_hex(
    history_context: ProjectContext, fake_clock: Callable[[], datetime]
) -> None:
    store = open_history(history_context, clock=fake_clock, entropy=lambda size: "a1b2")

    created = store.create_run(history_context, TextMission("Explore"))

    assert created.record.id == "run_20261030_143205_a1b2"
    assert created.record.started_at == "2026-10-30T17:32:05.000000Z"


def test_run_id_collision_draws_new_entropy(
    history_context: ProjectContext, fake_clock: Callable[[], datetime]
) -> None:
    values = iter(["aaaa", "aaaa", "bbbb"])
    store = open_history(history_context, clock=fake_clock, entropy=lambda size: next(values))
    first = store.create_run(history_context, TextMission("Um"))
    store.finish_run(first.record.id, RunStatus.COMPLETED)

    second = store.create_run(history_context, TextMission("Dois"))

    assert first.record.id == "run_20261030_143205_aaaa"
    assert second.record.id == "run_20261030_143205_bbbb"


def test_file_mission_is_snapshotted_before_handoff(
    history_context: ProjectContext, tmp_path: Path
) -> None:
    original = "# Missão\nCadastrar funcionário com e-mail opcional\n".encode()
    mission = tmp_path / "employee.md"
    mission.write_bytes(original)
    store = open_history(history_context)

    created = store.create_run(history_context, FileMission(mission))
    mission.write_text("conteúdo alterado depois do início", encoding="utf-8")

    record = created.record
    assert record.mission_source == MissionSource.FILE
    assert record.mission_text is None
    assert record.mission_snapshot_path == "mission/source.md"
    assert record.mission_file_sha256 == hashlib.sha256(original).hexdigest()
    snapshot = created.paths.mission_snapshot
    assert snapshot is not None
    assert snapshot == created.paths.run_dir / "mission" / "source.md"
    assert snapshot.read_bytes() == original
    mission.unlink()
    assert store.get_run(record.id).mission_file_sha256 == hashlib.sha256(original).hexdigest()


def test_run_creation_does_not_create_report(
    history_context: ProjectContext, tmp_path: Path
) -> None:
    store = open_history(history_context)
    text_run = store.create_run(history_context, TextMission("Explore"))
    store.finish_run(text_run.record.id, RunStatus.COMPLETED)
    mission = tmp_path / "m.md"
    mission.write_text("Explore o cadastro", encoding="utf-8")
    file_run = store.create_run(history_context, FileMission(mission))

    for created in (text_run, file_run):
        assert created.paths.evidence_dir.is_dir()
        assert not (created.paths.run_dir / "report.md").exists()
    assert text_run.paths.mission_snapshot is None
    assert not (text_run.paths.run_dir / "mission").exists()
    assert file_run.paths.mission_snapshot is not None
    assert file_run.paths.mission_snapshot.is_file()


def test_snapshot_removes_credentials_and_keeps_run_limits(
    history_context: ProjectContext,
) -> None:
    store = open_history(history_context)

    record = store.create_run(history_context, TextMission("Explore")).record

    snapshot = record.config_snapshot
    text = json.dumps(snapshot)
    assert "credentials" not in text
    assert "HOMOLOG_USERNAME" not in text
    assert "HOMOLOG_PASSWORD" not in text
    assert snapshot["run_limits"] == {"max_actions": 40, "max_minutes": 30}
    assert snapshot["profile"] == "homolog"
    assert snapshot["project_id"] == history_context.require_project_id()
    environment = snapshot["environment"]
    assert isinstance(environment, dict)
    assert environment["target_url"] == "https://homolog.example.test"
    assert environment["denied_methods"] == ["DELETE"]


def test_process_lost_marks_every_non_terminal_state_interrupted(
    history_context: ProjectContext,
) -> None:
    owner = open_history(history_context, owner_pid=LOST_PID, process_checker=lambda pid: False)
    waiting_approval = owner.create_run(history_context, TextMission("a")).record.id
    owner.transition_run(waiting_approval, RunStatus.AWAITING_APPROVAL)
    waiting_clarification = owner.create_run(history_context, TextMission("b")).record.id
    owner.transition_run(waiting_clarification, RunStatus.AWAITING_CLARIFICATION)
    running = owner.create_run(history_context, TextMission("c")).record.id
    completed = owner.create_run(history_context, TextMission("d")).record.id
    owner.finish_run(completed, RunStatus.COMPLETED)
    completed_before = owner.get_run(completed)

    restarted = open_history(history_context, process_checker=lambda pid: pid != LOST_PID)

    for run_id in (waiting_approval, waiting_clarification, running):
        record = restarted.get_run(run_id)
        assert record.status == RunStatus.INTERRUPTED
        assert record.status_reason == PROCESS_LOST_REASON
        assert record.ended_at is not None
        last = restarted.list_events(run_id)[-1]
        assert last.event_type == EventType.RUN_ENDED
        assert last.payload == {"status": "interrupted", "reason": "process_lost"}
    assert restarted.get_run(completed) == completed_before
    assert restarted.get_active_run() is None
    assert not (history_context.project_data_dir / "active-run.json").exists()


def test_live_owner_keeps_run_active(history_context: ProjectContext) -> None:
    store = open_history(history_context, owner_pid=LOST_PID, process_checker=lambda pid: True)
    created = store.create_run(history_context, TextMission("Explore"))

    reopened = open_history(history_context, process_checker=lambda pid: True)

    active = reopened.get_active_run()
    assert active is not None and active.id == created.record.id
    assert reopened.get_active_run(created.record.id) == active
    assert reopened.get_active_run("run_20000101_000000_ffff") is None
    with pytest.raises(ActiveRunExistsError):
        reopened.create_run(history_context, TextMission("Outra"))


def test_active_run_requires_coherent_pointer_database_and_process(
    history_context: ProjectContext,
) -> None:
    alive = {LOST_PID}
    store = open_history(
        history_context, owner_pid=LOST_PID, process_checker=lambda pid: pid in alive
    )
    created = store.create_run(history_context, TextMission("Explore"))
    assert store.get_active_run() is not None

    alive.clear()
    assert store.get_active_run() is None

    alive.add(LOST_PID)
    (history_context.project_data_dir / "active-run.json").write_text("{", encoding="utf-8")
    assert store.get_active_run() is None

    store.finish_run(created.record.id, RunStatus.CANCELLED, "pedido do usuário")
    assert store.get_active_run() is None


def test_finish_writes_run_ended_and_terminal_is_immutable(
    history_context: ProjectContext,
) -> None:
    store = open_history(history_context)
    run_id = store.create_run(history_context, TextMission("Explore")).record.id
    store.transition_run(run_id, RunStatus.AWAITING_APPROVAL)
    store.transition_run(run_id, RunStatus.RUNNING)

    record = store.finish_run(run_id, RunStatus.LIMIT_REACHED, "max_actions")

    assert record.status == RunStatus.LIMIT_REACHED
    assert record.ended_at is not None
    assert [event.event_type for event in store.list_events(run_id)] == [
        EventType.RUN_STARTED,
        EventType.RUN_ENDED,
    ]
    assert not (history_context.project_data_dir / "active-run.json").exists()
    with pytest.raises(InvalidTransitionError):
        store.transition_run(run_id, RunStatus.RUNNING)
    with pytest.raises(InvalidTransitionError):
        store.finish_run(run_id, RunStatus.RUNNING)
    conn = _connect(store.database_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE runs SET status = 'running', ended_at = NULL WHERE id = ?", (run_id,)
            )
    finally:
        conn.close()


@pytest.mark.parametrize(
    "mission",
    [TextMission(""), TextMission("   "), TextMission("x" * 10_001)],
)
def test_invalid_text_mission_creates_nothing(
    history_context: ProjectContext, mission: TextMission
) -> None:
    store = open_history(history_context)

    with pytest.raises(InvalidMissionError):
        store.create_run(history_context, mission)

    assert store.list_runs() == []
    assert not (history_context.project_data_dir / "runs").exists()


def test_text_mission_accepts_ten_thousand_characters(history_context: ProjectContext) -> None:
    store = open_history(history_context)

    record = store.create_run(history_context, TextMission("x" * 10_000)).record

    assert record.mission_text == "x" * 10_000


def test_mission_file_must_be_regular_readable_file(
    history_context: ProjectContext, tmp_path: Path
) -> None:
    store = open_history(history_context)

    for path in (tmp_path / "inexistente.md", tmp_path):
        with pytest.raises(InvalidMissionError):
            store.create_run(history_context, FileMission(path))

    assert store.list_runs() == []
