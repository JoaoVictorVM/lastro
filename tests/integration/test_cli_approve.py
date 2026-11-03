import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from typer.testing import CliRunner

from lastro.cli.app import app
from lastro.config.loader import ProjectContext
from lastro.history import (
    ApprovalStatus,
    EventType,
    HistoryStore,
    JsonObject,
    RunStatus,
    TextMission,
    open_history,
)

FINGERPRINT = "a" * 64


def pending(context: ProjectContext) -> tuple[HistoryStore, str]:
    history = open_history(context)
    run_id = history.create_run(context, TextMission("Editar")).record.id
    payload: JsonObject = {
        "summary": "edit employee #57 (phone, email) in Clínica Aurora",
        "run_id": run_id,
        "changes": [{"field": "phone", "old": "1", "new": "2"}],
        "reason": "Validar persistência",
        "components": {},
    }
    history.request_approval(run_id, "act57", FINGERPRINT, payload)
    return history, run_id


def test_approve_shows_request_and_records_decision(
    runner: CliRunner, history_context: ProjectContext
) -> None:
    history, run_id = pending(history_context)

    result = runner.invoke(app, ["approve", "act57", "--note", "ok"])

    assert result.exit_code == 0, result.output
    assert "Lastro needs approval: edit employee #57 (phone, email) in Clínica Aurora" in (
        result.stdout
    )
    assert "  - phone: 1 -> 2" in result.stdout
    assert "approved by user at " in result.stdout
    record = history.get_approval("act57")
    assert record is not None and record.status == ApprovalStatus.APPROVED
    assert record.decision_reason == "ok"
    assert history.get_run(run_id).status == RunStatus.RUNNING


def test_unknown_expired_or_decided_id_keeps_state(
    runner: CliRunner, history_context: ProjectContext
) -> None:
    history, run_id = pending(history_context)
    events = len(history.list_events(run_id))

    unknown = runner.invoke(app, ["approve", "nope"])
    assert unknown.exit_code == 1
    assert unknown.stderr.strip() == "No pending approval with id nope."
    assert len(history.list_events(run_id)) == events

    assert runner.invoke(app, ["approve", "act57", "--skip"]).exit_code == 0
    again = runner.invoke(app, ["approve", "act57"])
    assert again.exit_code == 1
    assert again.stderr.strip() == "No pending approval with id act57."
    record = history.get_approval("act57")
    assert record is not None and record.status == ApprovalStatus.SKIPPED


def test_skip_and_end_are_exclusive_and_note_is_limited(
    runner: CliRunner, history_context: ProjectContext
) -> None:
    history, _ = pending(history_context)

    both = runner.invoke(app, ["approve", "act57", "--skip", "--end"])
    long_note = runner.invoke(app, ["approve", "act57", "--note", "x" * 2001])

    assert both.exit_code == 2
    assert "Use --skip or --end, not both." in both.stderr
    assert long_note.exit_code == 2
    record = history.get_approval("act57")
    assert record is not None and record.status == ApprovalStatus.PENDING


def test_end_decision_cancels_and_builds_partial_report(
    runner: CliRunner, history_context: ProjectContext
) -> None:
    history, run_id = pending(history_context)

    result = runner.invoke(app, ["approve", "act57", "--end"])

    assert result.exit_code == 0, result.output
    run = history.get_run(run_id)
    assert (run.status, run.status_reason) == (RunStatus.CANCELLED, "ended_at_approval_gate")
    assert "cancelled; no further writes" in result.stdout
    report_line = next(line for line in result.stdout.splitlines() if "Partial report" in line)
    report = Path(report_line.split(": ", 1)[1])
    assert report.is_file()
    assert r"Status: cancelled (ended\_at\_approval\_gate)" in report.read_text(encoding="utf-8")
    snapshots = history.list_report_snapshots(run_id)
    assert [item.kind.value for item in snapshots] == ["partial"]
    assert history.get_active_run() is None


def test_two_terminals_cannot_decide_same_request_twice(
    history_context: ProjectContext, workdir: Path
) -> None:
    history, run_id = pending(history_context)
    barrier = threading.Barrier(3)
    codes: list[int] = []

    def terminal(arguments: list[str]) -> None:
        barrier.wait()
        completed = subprocess.run(
            [sys.executable, "-m", "lastro", *arguments],
            cwd=workdir,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
        codes.append(completed.returncode)

    threads = [
        threading.Thread(target=terminal, args=(args,))
        for args in (["approve", "act57"], ["approve", "act57", "--skip"], ["approve", "act57"])
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(codes) == [0, 1, 1]
    decided = [
        event
        for event in history.list_events(run_id)
        if event.event_type == EventType.APPROVAL_DECIDED
    ]
    assert len(decided) == 1
    assert isinstance(decided[0].payload, dict)
    assert decided[0].payload["decided_by"] == "cli"
    assert json.dumps(decided[0].payload).count("act57") == 1
