import re

import pytest
from typer.testing import CliRunner

from lastro.cli import tail_cmd
from lastro.cli.app import app
from lastro.config.loader import ProjectContext
from lastro.history import EventType, HistoryStore, RunStatus, TextMission, open_history

ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture
def store(history_context: ProjectContext) -> HistoryStore:
    return open_history(history_context)


@pytest.fixture
def run_id(store: HistoryStore, history_context: ProjectContext) -> str:
    return store.create_run(history_context, TextMission("Explore")).record.id


def _event_types(output: str) -> list[str]:
    return [line.split()[1] for line in ANSI.sub("", output).splitlines() if line.strip()]


def test_tail_prints_events_in_sequence_and_drains_terminal_run(
    runner: CliRunner, store: HistoryStore, run_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    polls: list[float] = []

    def late_events(seconds: float) -> None:
        polls.append(seconds)
        if len(polls) == 1:
            store.append_event(run_id, EventType.ORACLE_SIGNAL, payload={"status": 500})
            store.append_event(run_id, EventType.TOOL_CALL, tool_name="click", arguments={"n": 1})
        elif len(polls) == 2:
            store.append_event(
                run_id, EventType.POLICY_DECISION, tool_name="click", policy_decision="allow"
            )
        else:
            store.append_event(run_id, EventType.LIMIT_HIT, payload={"limit": "max_actions"})
            store.finish_run(run_id, RunStatus.LIMIT_REACHED, "max_actions")

    monkeypatch.setattr(tail_cmd, "sleep", late_events)

    result = runner.invoke(app, ["tail"])

    assert result.exit_code == 0, result.output
    assert _event_types(result.stdout) == [
        "run_started",
        "oracle_signal",
        "tool_call",
        "policy_decision",
        "limit_hit",
        "run_ended",
    ]
    assert polls == [tail_cmd.POLL_INTERVAL_SECONDS] * 3
    first = result.stdout.splitlines()[0]
    assert re.match(r"^\d{2}:\d{2}:\d{2}  run_started  ", first)


def test_tail_of_finished_run_prints_everything_without_waiting(
    runner: CliRunner, store: HistoryStore, run_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.append_event(run_id, EventType.ORACLE_SIGNAL, payload={"status": 404})
    store.finish_run(run_id, RunStatus.COMPLETED)
    monkeypatch.setattr(tail_cmd, "sleep", lambda seconds: pytest.fail("não deveria esperar"))

    result = runner.invoke(app, ["tail", run_id])

    assert result.exit_code == 0, result.output
    assert _event_types(result.stdout) == ["run_started", "oracle_signal", "run_ended"]


def test_tail_colors_policy_decisions_only_on_terminal(
    runner: CliRunner, store: HistoryStore, run_id: str
) -> None:
    for decision in ("allow", "require_approval", "deny"):
        store.append_event(
            run_id, EventType.POLICY_DECISION, tool_name="click", policy_decision=decision
        )
    store.finish_run(run_id, RunStatus.COMPLETED)

    colored = runner.invoke(app, ["tail", run_id], color=True)
    plain = runner.invoke(app, ["tail", run_id])

    decisions = [line for line in colored.stdout.splitlines() if "click" in line]
    expected_colors = {"allow": "\x1b[32m", "require_approval": "\x1b[33m", "deny": "\x1b[31m"}
    assert len(decisions) == 3
    for line, (decision, color) in zip(decisions, expected_colors.items(), strict=True):
        assert line.endswith(f"{color}\x1b[1m{decision}\x1b[0m")
    assert ANSI.search(plain.stdout) is None
    assert ANSI.sub("", colored.stdout) == plain.stdout


def test_ctrl_c_ends_only_the_tail(
    runner: CliRunner, store: HistoryStore, run_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupt(seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(tail_cmd, "sleep", interrupt)

    result = runner.invoke(app, ["tail"])

    assert result.exit_code == 0, result.output
    assert _event_types(result.stdout) == ["run_started"]
    assert store.get_run(run_id).status == RunStatus.RUNNING
    assert store.get_active_run() is not None


def test_tail_without_active_run_explains_how_to_choose(
    runner: CliRunner, history_context: ProjectContext
) -> None:
    result = runner.invoke(app, ["tail"])

    assert result.exit_code == 1
    assert tail_cmd.NO_ACTIVE_RUN_MESSAGE in result.stderr


def test_tail_of_unknown_run_fails(runner: CliRunner, history_context: ProjectContext) -> None:
    result = runner.invoke(app, ["tail", "run_20000101_000000_ffff"])

    assert result.exit_code == 1
    assert "run não encontrada" in result.stderr
