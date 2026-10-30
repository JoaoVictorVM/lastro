import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta

import pytest
from typer.testing import CliRunner

from lastro.cli import history_cmd
from lastro.cli.app import app
from lastro.config.loader import ProjectContext
from lastro.history import HistoryStore, IssueMission, RunStatus, TextMission, open_history

HASH = "b" * 64


@pytest.fixture
def store(
    history_context: ProjectContext,
    fake_clock: Callable[[], datetime],
    monkeypatch: pytest.MonkeyPatch,
) -> HistoryStore:
    monkeypatch.setattr(history_cmd, "clock", fake_clock)
    return open_history(history_context, clock=fake_clock)


def _run_at(
    store: HistoryStore,
    context: ProjectContext,
    clock_now: list[datetime],
    days_ago: float,
    status: RunStatus,
    minutes: int = 10,
) -> str:
    now = clock_now[0]
    clock_now[0] = now - timedelta(days=days_ago)
    run_id = store.create_run(context, TextMission("Explore")).record.id
    clock_now[0] += timedelta(minutes=minutes)
    store.finish_run(run_id, status)
    clock_now[0] = now
    return run_id


def _ids(output: str) -> list[str]:
    return [line.split()[0] for line in output.splitlines()[1:]]


def test_history_filters_blocked_since_seven_days(
    runner: CliRunner,
    store: HistoryStore,
    history_context: ProjectContext,
    clock_now: list[datetime],
) -> None:
    recent_blocked = _run_at(store, history_context, clock_now, 2, RunStatus.BLOCKED)
    _run_at(store, history_context, clock_now, 10, RunStatus.BLOCKED)
    _run_at(store, history_context, clock_now, 1, RunStatus.COMPLETED)
    _run_at(store, history_context, clock_now, 3, RunStatus.INTERRUPTED)
    edge_blocked = _run_at(store, history_context, clock_now, 6.99, RunStatus.BLOCKED)

    result = runner.invoke(app, ["history", "list", "--status", "blocked", "--since", "7d"])

    assert result.exit_code == 0, result.output
    assert _ids(result.stdout) == [recent_blocked, edge_blocked]
    assert all(" blocked " in line for line in result.stdout.splitlines()[1:])


def test_history_table_shows_aggregates_in_local_time(
    runner: CliRunner,
    store: HistoryStore,
    history_context: ProjectContext,
    clock_now: list[datetime],
) -> None:
    run_id = _run_at(store, history_context, clock_now, 0, RunStatus.COMPLETED, minutes=75)
    conn = sqlite3.connect(store.database_path)
    rows = [
        ("f1", "confirmed"),
        ("f2", "suspected"),
        ("f3", "suspected"),
        ("f4", "inconclusive"),
        ("f5", None),
    ]
    conn.executemany(
        "INSERT INTO findings (id, run_id, classification, created_at, updated_at) "
        "VALUES (?, ?, ?, 'x', 'x')",
        [(fid, run_id, classification) for fid, classification in rows],
    )
    conn.executemany(
        "INSERT INTO created_records (id, run_id, entity_type, system_identifier, tenant, "
        "cleanup_status, created_at, cleaned_at) VALUES (?, ?, 'employee', ?, 'acme', ?, 'x', ?)",
        [
            ("r1", run_id, "1", "pending", None),
            ("r2", run_id, "2", "pending", None),
            ("r3", run_id, "3", "cleaned_manually", "y"),
        ],
    )
    conn.executemany(
        "INSERT INTO criteria (id, run_id, payload_json, created_at, updated_at) "
        "VALUES (?, ?, ?, 'x', 'x')",
        [
            ("c1", run_id, '{"flow":"cadastro"}'),
            ("c2", run_id, '{"flow":"busca"}'),
            ("c3", run_id, '{"flow":"cadastro"}'),
        ],
    )
    conn.commit()
    conn.close()

    result = runner.invoke(app, ["history", "list"])

    assert result.exit_code == 0, result.output
    header, row = result.stdout.splitlines()
    assert header.split() == [
        "RUN",
        "INÍCIO",
        "DURAÇÃO",
        "STATUS",
        "PERFIL",
        "FLUXOS",
        "FINDINGS",
        "C/S/I",
        "LIMPEZA",
    ]
    assert row.split() == [
        run_id,
        "2026-10-30",
        "14:32:05",
        "1:15:00",
        "completed",
        "homolog",
        "busca,",
        "cadastro",
        "1/2/1",
        "2",
    ]

    flow = runner.invoke(app, ["history", "list", "--flow", "busca"])
    other_flow = runner.invoke(app, ["history", "list", "--flow", "login"])
    assert _ids(flow.stdout) == [run_id]
    assert other_flow.stdout.strip() == "Nenhuma run encontrada."


def test_history_filters_combine_with_and_and_order_by_start(
    runner: CliRunner,
    store: HistoryStore,
    history_context_for: Callable[[str], ProjectContext],
    clock_now: list[datetime],
) -> None:
    homolog, local = history_context_for("homolog"), history_context_for("local")
    old = _run_at(store, homolog, clock_now, 3, RunStatus.COMPLETED)
    new = _run_at(store, homolog, clock_now, 1, RunStatus.COMPLETED)
    local_run = _run_at(store, local, clock_now, 2, RunStatus.COMPLETED)

    everything = runner.invoke(app, ["history", "list"])
    by_profile = runner.invoke(app, ["history", "list", "--profile", "homolog"])
    limited = runner.invoke(app, ["history", "list", "--limit", "1"])
    combined = runner.invoke(
        app, ["history", "list", "--profile", "local", "--status", "completed", "--since", "1d"]
    )

    assert _ids(everything.stdout) == [new, local_run, old]
    assert _ids(by_profile.stdout) == [new, old]
    assert _ids(limited.stdout) == [new]
    assert combined.stdout.strip() == "Nenhuma run encontrada."


def test_history_filters_by_issue(
    runner: CliRunner,
    store: HistoryStore,
    history_context: ProjectContext,
    clock_now: list[datetime],
) -> None:
    conn = sqlite3.connect(store.database_path)
    conn.execute(
        "INSERT INTO issue_snapshots (id, project_id, repository, issue_number, content_sha256, "
        "captured_at) VALUES ('iss_42', ?, 'org/repo', 42, ?, 'x')",
        (history_context.require_project_id(), HASH),
    )
    conn.commit()
    conn.close()
    issue_run = store.create_run(history_context, IssueMission("iss_42", "Corrigir")).record.id
    store.finish_run(issue_run, RunStatus.COMPLETED)
    _run_at(store, history_context, clock_now, 1, RunStatus.COMPLETED)

    for value in ("42", "#42", "iss_42"):
        result = runner.invoke(app, ["history", "list", "--issue", value])
        assert _ids(result.stdout) == [issue_run], value
    assert "Nenhuma" in runner.invoke(app, ["history", "list", "--issue", "7"]).stdout


def test_history_default_limit_is_twenty(
    runner: CliRunner,
    store: HistoryStore,
    history_context: ProjectContext,
    clock_now: list[datetime],
) -> None:
    for index in range(21):
        _run_at(store, history_context, clock_now, index / 24, RunStatus.COMPLETED, minutes=1)

    result = runner.invoke(app, ["history", "list"])

    assert len(_ids(result.stdout)) == 20


@pytest.mark.parametrize(
    "args",
    [["--status", "quebrado"], ["--since", "ontem"], ["--limit", "0"]],
)
def test_history_rejects_invalid_filters(
    runner: CliRunner, history_context: ProjectContext, args: list[str]
) -> None:
    result = runner.invoke(app, ["history", "list", *args])

    assert result.exit_code == 2
    assert not history_context.project_data_dir.exists()


def test_since_accepts_relative_and_iso_values() -> None:
    now = datetime.fromisoformat("2026-10-30T17:32:05+00:00")

    assert history_cmd.parse_since("7d", now, "UTC") == now - timedelta(days=7)
    assert history_cmd.parse_since("12h", now, "UTC") == now - timedelta(hours=12)
    assert history_cmd.parse_since("30m", now, "UTC") == now - timedelta(minutes=30)
    assert history_cmd.parse_since(
        "2026-10-01", now, "America/Sao_Paulo"
    ) == datetime.fromisoformat("2026-10-01T00:00:00-03:00")
