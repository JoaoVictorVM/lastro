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


def test_ctrl_c_requests_cancellation_of_followed_run(
    runner: CliRunner, store: HistoryStore, run_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lastro.execution import supervisor

    def interrupt(seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(tail_cmd, "sleep", interrupt)
    monkeypatch.setattr(supervisor, "owner_alive", lambda store, record: False)

    result = runner.invoke(app, ["tail"])

    assert result.exit_code == 0, result.output
    assert _event_types(result.stdout)[0] == "run_started"
    assert "Run cancelled. Partial report:" in result.stdout
    assert store.get_run(run_id).status == RunStatus.CANCELLED
    assert store.get_active_run() is None


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


def test_tail_formats_oracle_action_count_and_background(
    runner: CliRunner,
    store: HistoryStore,
    run_id: str,
) -> None:
    from lastro.oracles.models import OracleOccurrence

    action = store.append_event(run_id, EventType.TOOL_CALL, tool_name="submit", correlation_id="a")
    occurrence = OracleOccurrence(
        occurrence_id="one",
        signal_type="http_5xx",
        url="https://example.test/api/employees/12",
        url_path="/api/employees/12",
        http_method="PUT",
        http_status=500,
        message="HTTP 500",
        timestamp="2026-11-01T15:00:04.000000Z",
        action_id="a",
        action_sequence=action.sequence,
    )
    store.record_oracle_signal(run_id, occurrence)
    store.record_oracle_signal(run_id, occurrence.model_copy(update={"occurrence_id": "two"}))
    store.record_oracle_signal(
        run_id,
        OracleOccurrence(
            occurrence_id="background",
            signal_type="js_error",
            url=None,
            url_path="",
            http_method=None,
            http_status=None,
            message="bad\n\x1b[31mtext\x1b[0m\r\x07",
            timestamp="2026-11-01T15:00:05.000000Z",
            action_id=None,
            action_sequence=None,
        ),
    )
    store.finish_run(run_id, "completed")
    result = runner.invoke(app, ["tail", run_id])
    assert result.exit_code == 0, result.output
    assert (
        f"ORACLE http_5xx PUT /api/employees/12 -> 500 (after action #{action.sequence})"
        in result.stdout
    )
    assert "occurrences: 2" in result.stdout
    assert "no action (background)" in result.stdout
    assert "bad text" in result.stdout
    assert "\x1b" not in result.stdout and "\x07" not in result.stdout


def test_tail_rings_only_for_new_pending_request_in_tty(
    runner: CliRunner, store: HistoryStore, run_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = "edit employee #57 (phone, email) in Clínica Aurora"
    store.request_approval(run_id, "old", "a" * 64, {"summary": "old request"})
    store.decide_approval("old", "skip", "cli")
    polls: list[float] = []

    def next_poll(seconds: float) -> None:
        polls.append(seconds)
        if len(polls) == 1:
            store.request_approval(run_id, "act57", "b" * 64, {"summary": summary})
        elif len(polls) == 2:
            store.decide_approval("act57", "approve", "cli")
        else:
            store.finish_run(run_id, RunStatus.COMPLETED, "done")

    monkeypatch.setattr(tail_cmd, "sleep", next_poll)
    monkeypatch.setattr(tail_cmd, "is_tty", lambda: True)

    result = runner.invoke(app, ["tail"])

    assert result.exit_code == 0, result.output
    plain = ANSI.sub("", result.stdout)
    assert plain.count("\a") == 1
    assert plain.count("==== APPROVAL REQUIRED ====") == 2
    assert f"  {summary}" in plain
    assert "Decide: lastro approve act57 [--skip | --end] (expires " in plain
    assert re.search(r"approved by user at \d\d:\d\d \(act57\)", plain)
    assert re.search(r"skipped by user at \d\d:\d\d \(old\)", plain)


def test_tail_without_tty_never_rings_and_shows_degraded_fallback(
    runner: CliRunner, store: HistoryStore, run_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = store.request_approval(run_id, "act1", "a" * 64, {"summary": "edit employee #1"})
    store.record_notification(
        record,
        "desktop",
        "unsupported",
        requested_at=store._clock(),  # pyright: ignore[reportPrivateUsage]
        latency_ms=3,
        error_code="notifier_not_found",
    )
    store.decide_approval("act1", "end_run", "cli")
    monkeypatch.setattr(tail_cmd, "is_tty", lambda: False)

    result = runner.invoke(app, ["tail", run_id])

    plain = ANSI.sub("", result.stdout)
    assert "\a" not in plain
    assert "notification degraded (desktop: notifier_not_found)" in plain
    assert re.search(r"run ended by user at \d\d:\d\d \(act1\)", plain)
