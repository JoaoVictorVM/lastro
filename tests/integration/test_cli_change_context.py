import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from lastro.change_context import issues
from lastro.change_context.issues import GhResult
from lastro.change_context.models import canonical, sha256_text
from lastro.cli import run_cmd
from lastro.cli.app import app
from lastro.config.loader import ProjectContext
from lastro.history import CreatedRun, MissionSource, RunStatus, open_history

from .conftest import GitRepo

ISSUE_URL = "https://github.com/org/repo/issues/123"
START = ["--profile", "homolog", "run", "start"]


def fake_gh(status: int = 0, body: str = "Critério: salvar sem email.") -> Any:
    calls: list[list[str]] = []

    def run(args: Sequence[str], timeout: float) -> GhResult:
        calls.append(list(args))
        if status:
            return GhResult(1, b"", f"gh: error (HTTP {status})".encode())
        data = {
            "title": "Email opcional",
            "body": body,
            "labels": [{"name": "bug"}],
            "comments": 0,
            "updated_at": "2026-11-04T12:00:00Z",
            "html_url": ISSUE_URL,
        }
        return GhResult(0, json.dumps(data).encode(), b"")

    run.calls = calls  # type: ignore[attr-defined]
    return run


@pytest.fixture
def handoffs(monkeypatch: pytest.MonkeyPatch, history_context: ProjectContext) -> list[Any]:
    """No handoff a run, o snapshot e o contexto já precisam estar confirmados."""
    seen: list[Any] = []

    def record(created: CreatedRun) -> None:
        store = open_history(history_context)
        run = store.get_run(created.record.id)
        events = store.list_events(run.id)
        seen.append((run, store.get_change_context(run.id), [e.event_type for e in events]))

    monkeypatch.setattr(run_cmd, "handoff", record)
    return seen


def _finish(context: ProjectContext) -> None:
    store = open_history(context)
    active = store.get_active_run()
    if active is not None:
        store.finish_run(active.id, RunStatus.COMPLETED)


def test_issue_url_stores_snapshot_before_handoff(
    runner: CliRunner,
    history_context: ProjectContext,
    handoffs: list[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = fake_gh()
    monkeypatch.setattr(issues, "run_gh", gh)

    result = runner.invoke(app, [*START, "--issue", ISSUE_URL])

    assert result.exit_code == 0, result.output
    assert "Issue: org/repo#123" in result.output
    run, context, events = handoffs[0]
    assert run.mission_source == MissionSource.ISSUE and run.issue_snapshot_id
    assert context is not None and context.issue_snapshot_id == run.issue_snapshot_id
    assert events == ["run_started", "change_context_collected"]
    snapshot = open_history(history_context).get_issue_snapshot(run.issue_snapshot_id)
    payload = {
        key: value
        for key, value in snapshot.payload.items()
        if key not in ("capture_started_at", "capture_finished_at", "scan_level")
    }
    assert sha256_text(canonical(payload)) == snapshot.content_sha256
    assert all(call[call.index("--method") + 1] == "GET" for call in gh.calls)


@pytest.mark.parametrize("status", [403, 404])
def test_issue_403_404_creates_no_run(
    runner: CliRunner,
    history_context: ProjectContext,
    handoffs: list[Any],
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    monkeypatch.setattr(issues, "run_gh", fake_gh(status))

    result = runner.invoke(app, [*START, "--issue", ISSUE_URL])

    assert result.exit_code == 2
    assert (
        f"Could not read issue (HTTP {status}). Check 'gh auth status' and repository access."
        in result.stderr
    )
    assert handoffs == []
    assert open_history(history_context).list_runs() == []


def test_sources_are_mutually_exclusive(
    runner: CliRunner, history_context: ProjectContext, workdir: Path
) -> None:
    exported = workdir / "issue.md"
    exported.write_text("# Cadastro\nEmail opcional\n", encoding="utf-8")
    combos = (
        ["--mission", "x", "--issue", ISSUE_URL],
        ["--issue-text", "x", "--issue-file", str(exported)],
        ["--mission-file", str(exported), "--issue-text", "x"],
        [],
    )
    for combo in combos:
        result = runner.invoke(app, [*START, *combo])
        assert result.exit_code == 2, combo
        assert "Informe exatamente uma fonte de missão" in result.stderr
    for flags in (["--compare", "feature"], ["--fetch"], ["--base", "main"]):
        result = runner.invoke(app, [*START, "--mission", "x", *flags])
        assert result.exit_code == 2 and "require --source" in result.stderr
    assert not history_context.project_data_dir.exists()


def test_offline_issue_inputs_start_runs(
    runner: CliRunner, history_context: ProjectContext, handoffs: list[Any], workdir: Path
) -> None:
    exported = workdir / "issue.md"
    exported.write_text("# Cadastro\n\nEmail opcional\n", encoding="utf-8")
    assert runner.invoke(app, [*START, "--issue-file", str(exported)]).exit_code == 0
    _finish(history_context)
    assert runner.invoke(app, [*START, "--issue-text", "Email opcional"]).exit_code == 0
    store = open_history(history_context)
    kinds = [store.get_issue_snapshot(run.issue_snapshot_id).source_kind for run, _, _ in handoffs]
    assert kinds == ["markdown", "text"]


def _enable_source(context: ProjectContext, extra: str = "") -> None:
    path = context.config_path
    path.write_text(
        path.read_text(encoding="utf-8")
        + f'\n[source]\nallow_paths = ["src/**"]\nbase_ref = "main"\n{extra}',
        encoding="utf-8",
    )


def test_source_run_freezes_change_context(
    runner: CliRunner,
    history_context: ProjectContext,
    handoffs: list[Any],
    git_repo: GitRepo,
    gitleaks: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_source(history_context)
    git_repo.write("src/form.py", "def save(email=None):\n    return email\n")
    monkeypatch.setenv("SOURCE_REPO_PATH", str(git_repo.root))

    result = runner.invoke(app, [*START, "--mission", "Explore", "--source", "--flow", "cadastro"])

    assert result.exit_code == 0, result.output
    assert "Change context: available" in result.output
    run, context, events = handoffs[0]
    assert context.flow_key == "cadastro"
    assert run.analyzed_code_version == git_repo.git("rev-parse", "HEAD")
    assert "change_context_collected" in events
    assert str(git_repo.root) not in json.dumps(context.payload)


def test_not_git_repository_warns_and_continues(
    runner: CliRunner,
    history_context: ProjectContext,
    handoffs: list[Any],
    gitleaks: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_source(history_context)
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("SOURCE_REPO_PATH", str(plain))

    result = runner.invoke(app, [*START, "--mission", "Explore", "--source"])

    assert result.exit_code == 0, result.output
    assert "change_context_skipped: not_git_repository" in result.output
    assert handoffs[0][1].source_state == "skipped"


def test_scanner_failure_creates_no_source_run(
    runner: CliRunner,
    history_context: ProjectContext,
    handoffs: list[Any],
    git_repo: GitRepo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lastro.change_context import scanner

    _enable_source(history_context)
    monkeypatch.setenv("SOURCE_REPO_PATH", str(git_repo.root))
    monkeypatch.setattr(scanner.shutil, "which", lambda name: None)

    result = runner.invoke(app, [*START, "--mission", "Explore", "--source"])

    assert result.exit_code == 2
    assert "gitleaks not found" in result.stderr
    assert "run without --source" in result.stderr
    assert handoffs == []
    assert open_history(history_context).list_runs() == []


def test_old_mission_options_still_work(
    runner: CliRunner, history_context: ProjectContext, handoffs: list[Any], workdir: Path
) -> None:
    mission = workdir / "mission.md"
    mission.write_text("Cadastrar funcionário", encoding="utf-8")
    assert runner.invoke(app, [*START, "--mission", "Explore"]).exit_code == 0
    _finish(history_context)
    assert runner.invoke(app, [*START, "--mission-file", str(mission)]).exit_code == 0
    assert [context for _, context, _ in handoffs] == [None, None]
    with sqlite3.connect(open_history(history_context).database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM change_contexts").fetchone()[0] == 0
