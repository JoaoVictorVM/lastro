import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from lastro.change_context import (
    ChangeContextDraft,
    ComparisonMode,
    FileOrigin,
    PreparedContext,
    SourceFile,
    SourceState,
)
from lastro.change_context.models import canonical, sha256_text
from lastro.config.loader import ProjectContext
from lastro.history import (
    HistoryError,
    IssueMission,
    RunStatus,
    TextMission,
    open_history,
)
from lastro.history import store as store_module

from .conftest import make_issue_capture


def _start_issue(history: Any, context: ProjectContext, capture: Any) -> Any:
    created = history.create_run(
        context,
        IssueMission(issue_snapshot_id="issue_" + capture.content.content_sha256[:16]),
        prepared=PreparedContext(issue=capture),
    )
    return created.record


def _db(history: Any) -> sqlite3.Connection:
    conn = sqlite3.connect(history.database_path)
    conn.row_factory = sqlite3.Row
    return conn


def test_changed_issue_preserves_old_snapshot(history_context: ProjectContext) -> None:
    history = open_history(history_context)
    first = _start_issue(history, history_context, make_issue_capture(body="Email obrigatório."))
    history.finish_run(first.id, RunStatus.COMPLETED)
    with _db(history) as conn:
        before = dict(conn.execute("SELECT * FROM issue_snapshots").fetchone())

    second = _start_issue(history, history_context, make_issue_capture(body="Email opcional."))

    assert second.issue_snapshot_id != first.issue_snapshot_id
    with _db(history) as conn:
        rows = conn.execute("SELECT * FROM issue_snapshots ORDER BY captured_at").fetchall()
        assert len(rows) == 2
        assert dict(rows[0]) == before
        old_run = conn.execute(
            "SELECT issue_snapshot_id FROM runs WHERE id = ?", (first.id,)
        ).fetchone()
    assert old_run["issue_snapshot_id"] == first.issue_snapshot_id
    old = history.get_issue_snapshot(first.issue_snapshot_id)
    assert old.payload["body"] == "Email obrigatório."


def test_issue_snapshot_hash_is_recomputable(history_context: ProjectContext) -> None:
    history = open_history(history_context)
    capture = make_issue_capture()
    run = _start_issue(history, history_context, capture)
    snapshot = history.get_issue_snapshot(run.issue_snapshot_id)
    payload = {
        key: value
        for key, value in snapshot.payload.items()
        if key not in ("capture_started_at", "capture_finished_at", "scan_level")
    }
    assert sha256_text(canonical(payload)) == snapshot.content_sha256
    assert snapshot.content_sha256 == capture.content.content_sha256


def test_same_content_reuses_snapshot_and_records_new_capture(
    history_context: ProjectContext,
) -> None:
    history = open_history(history_context)
    first = _start_issue(history, history_context, make_issue_capture())
    history.finish_run(first.id, RunStatus.COMPLETED)
    later = make_issue_capture()
    later = type(later)(
        content=later.content,
        capture_started_at="2026-11-05T09:00:00Z",
        capture_finished_at="2026-11-05T09:00:01Z",
        scan_level=later.scan_level,
    )

    second = _start_issue(history, history_context, later)

    assert second.issue_snapshot_id == first.issue_snapshot_id
    snapshot = history.get_issue_snapshot(first.issue_snapshot_id)
    assert snapshot.payload["capture_started_at"] == "2026-11-04T12:00:01Z"
    captures: dict[str, Any] = {}
    for run in (first, second):
        context = history.get_change_context(run.id)
        assert context is not None
        captures[run.id] = context.payload["issue_capture"]
    assert captures[second.id]["started_at"] == "2026-11-05T09:00:00Z"
    assert captures[first.id]["started_at"] == "2026-11-04T12:00:01Z"


def test_run_references_project_scoped_issue_snapshot(history_context: ProjectContext) -> None:
    history = open_history(history_context)
    run = _start_issue(history, history_context, make_issue_capture())
    with _db(history) as conn:
        row = conn.execute(
            "SELECT s.project_id FROM runs r JOIN issue_snapshots s ON s.id = r.issue_snapshot_id "
            "WHERE r.id = ?",
            (run.id,),
        ).fetchone()
        assert row["project_id"] == history.project_id
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.execute(
            "INSERT INTO issue_snapshots (id, project_id, repository, issue_number, source_kind, "
            "source_identity, content_sha256, payload_json, captured_at) "
            "VALUES ('issue_foreign', 'other-project', 'org/repo', 9, 'github', "
            "'github:org/repo#9', ?, '{}', '2026-11-04T00:00:00Z')",
            ("f" * 64,),
        )
    history.finish_run(run.id, RunStatus.COMPLETED)

    with pytest.raises(HistoryError):
        history.create_run(history_context, IssueMission(issue_snapshot_id="issue_foreign"))
    assert history.get_active_run() is None


def test_atomic_creation_rolls_back_all_references(
    history_context: ProjectContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = open_history(history_context)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.IntegrityError("simulated failure")

    monkeypatch.setattr(store_module, "insert_context", broken)
    with pytest.raises(sqlite3.IntegrityError):
        _start_issue(history, history_context, make_issue_capture())

    with _db(history) as conn:
        for table in ("runs", "issue_snapshots", "change_contexts", "artifacts", "events"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert not any((history.project_dir / "runs").iterdir())
    assert history.get_active_run() is None


def _draft(content: str) -> ChangeContextDraft:
    return ChangeContextDraft(
        source_state=SourceState.AVAILABLE,
        comparison=ComparisonMode.WORKING_TREE,
        head="a" * 40,
        sources=(
            SourceFile(
                file_id="file_" + "1" * 12,
                relative_path="src/form.py",
                origin=FileOrigin.COMMIT,
                revision_oid="b" * 40,
                content=content,
            ),
        ),
    )


def test_snapshot_context_and_sources_are_immutable(history_context: ProjectContext) -> None:
    history = open_history(history_context)
    capture = make_issue_capture()
    created = history.create_run(
        history_context,
        IssueMission(issue_snapshot_id="issue_immutable"),
        prepared=PreparedContext(issue=capture, change=_draft("print('ok')\n")),
    )
    statements = (
        "UPDATE issue_snapshots SET payload_json = '{}'",
        "DELETE FROM issue_snapshots",
        "UPDATE change_contexts SET source_state = 'skipped'",
        "DELETE FROM change_contexts",
        "UPDATE source_files SET size_bytes = 0",
        "DELETE FROM source_files",
    )
    with _db(history) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        for statement in statements:
            with pytest.raises(sqlite3.DatabaseError, match="immutable"):
                conn.execute(statement)
    source, text = history.read_source_file(created.record.id, "file_" + "1" * 12)
    assert text == "print('ok')\n"
    assert source.relative_path == "src/form.py"
    assert created.record.analyzed_code_version == "a" * 40


def test_text_mission_has_no_change_context(
    history_context_for: Callable[[str], ProjectContext],
) -> None:
    context = history_context_for("homolog")
    history = open_history(context)
    run = history.create_run(context, TextMission("Explore employees")).record
    assert history.get_change_context(run.id) is None
    assert run.issue_snapshot_id is None


def test_offline_snapshot_keeps_null_remote_identity(history_context: ProjectContext) -> None:
    history = open_history(history_context)
    run = _start_issue(
        history,
        history_context,
        make_issue_capture(kind="markdown", repository=None, number=None),
    )
    snapshot = history.get_issue_snapshot(run.issue_snapshot_id)
    assert (snapshot.repository, snapshot.issue_number, snapshot.source_kind) == (
        None,
        None,
        "markdown",
    )
    assert snapshot.payload["url"] is None


def test_context_artifacts_live_in_private_run_dir(history_context: ProjectContext) -> None:
    history = open_history(history_context)
    created = history.create_run(
        history_context,
        TextMission("Explore employees"),
        prepared=PreparedContext(change=_draft("x = 1\n")),
    )
    run_dir: Path = created.paths.run_dir
    assert (run_dir / "context" / "manifest.json").is_file()
    assert (run_dir / "context" / "sources" / ("file_" + "1" * 12 + ".txt")).is_file()
    paths = {item.relative_path for item in history.list_artifacts(created.record.id)}
    assert {"context/manifest.json", "context/summary.md", "context/changes.diff"} <= paths
