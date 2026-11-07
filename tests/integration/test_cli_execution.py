import re
from collections.abc import Callable
from datetime import datetime, timedelta

import pytest
from typer.testing import CliRunner

from lastro.cli import tail_cmd
from lastro.cli.app import app
from lastro.config.loader import ProjectContext
from lastro.execution import supervisor
from lastro.execution.supervisor import enforce_deadlines
from lastro.history import RunStatus, TextMission, open_history

ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture
def no_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    # Sem browser owner neste processo: a CLI finaliza pelo supervisor.
    monkeypatch.setattr(supervisor, "owner_alive", lambda store, record: False)


def test_cancel_defaults_to_active_run(
    runner: CliRunner, history_context: ProjectContext, no_owner: None
) -> None:
    history = open_history(history_context)
    run_id = history.create_run(history_context, TextMission("Explore")).record.id
    result = runner.invoke(app, ["--profile", "homolog", "run", "cancel"])
    assert result.exit_code == 0, result.output
    assert "Run cancelled. Partial report:" in result.output
    assert "report.partial.md" in result.output
    record = history.get_run(run_id)
    assert (record.status, record.status_reason) == (RunStatus.CANCELLED, "user_cancelled")
    again = runner.invoke(app, ["--profile", "homolog", "run", "cancel", run_id])
    assert again.exit_code == 0, again.output
    assert f"Run {run_id} already ended: cancelled (user_cancelled)." in again.output


def test_cancel_without_active_run_is_history_error(
    runner: CliRunner, history_context: ProjectContext
) -> None:
    open_history(history_context)
    result = runner.invoke(app, ["--profile", "homolog", "run", "cancel"])
    assert result.exit_code != 0
    assert "No active run" in result.output
    unknown = runner.invoke(app, ["--profile", "homolog", "run", "cancel", "run_x"])
    assert unknown.exit_code != 0


def test_tail_ctrl_c_requests_cancel(
    runner: CliRunner,
    history_context: ProjectContext,
    monkeypatch: pytest.MonkeyPatch,
    no_owner: None,
) -> None:
    history = open_history(history_context)
    run_id = history.create_run(history_context, TextMission("Explore")).record.id

    def interrupt(_: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(tail_cmd, "sleep", interrupt)
    result = runner.invoke(app, ["--profile", "homolog", "tail", run_id])
    assert result.exit_code == 0, result.output
    text = ANSI.sub("", result.output)
    assert "Cancelling run" in text
    assert "Run cancelled. Partial report:" in text
    assert history.get_run(run_id).status == RunStatus.CANCELLED


def test_tail_ctrl_c_on_finished_run_only_exits(
    runner: CliRunner, history_context: ProjectContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = open_history(history_context)
    run_id = history.create_run(history_context, TextMission("Explore")).record.id
    history.finish_run(run_id, RunStatus.COMPLETED)
    monkeypatch.setattr(tail_cmd, "sleep", lambda _: pytest.fail("não deveria esperar"))
    result = runner.invoke(app, ["--profile", "homolog", "tail", run_id])
    assert result.exit_code == 0
    assert "Cancelling" not in result.output


def test_tail_help_mentions_cancellation(runner: CliRunner) -> None:
    result = runner.invoke(app, ["tail", "--help"])
    assert "cancelamento" in ANSI.sub("", result.output)


def test_launch_pending_run_has_duration_supervisor(
    history_context: ProjectContext,
    clock_now: list[datetime],
    fake_clock: Callable[[], datetime],
) -> None:
    history = open_history(history_context, clock=fake_clock)
    created = history.create_run(
        history_context,
        TextMission("Explore"),
        executor_snapshot={"codex": {"launch_pending": True}},
    )
    run_id = created.record.id
    assert enforce_deadlines(history, now=fake_clock) == []
    clock_now[0] += timedelta(minutes=31)
    assert enforce_deadlines(history, now=fake_clock) == [run_id]
    record = history.get_run(run_id)
    assert (record.status, record.status_reason) == (RunStatus.LIMIT_REACHED, "duration_limit")
    assert history.execution_summary(run_id).report_kind == "partial"
