import sqlite3
from pathlib import Path

from lastro.config.loader import ProjectContext
from lastro.history import ArtifactRecord, ArtifactType, RunStatus, TextMission, open_history
from lastro.reporting.integrity import ProblemKind, verify_artifacts


def snapshot(path: Path) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(path)
    try:
        return [tuple(row) for row in conn.execute("SELECT * FROM artifacts ORDER BY id")]
    finally:
        conn.close()


def test_verify_reports_missing_and_tampered_files(history_context: ProjectContext) -> None:
    store = open_history(history_context)
    run_id = store.create_run(history_context, TextMission("Explore")).record.id
    kept = store.register_artifact(run_id, "evidence/a.png", ArtifactType.SCREENSHOT, content=b"a")
    tampered = store.register_artifact(
        run_id, "evidence/b.png", ArtifactType.SCREENSHOT, content=b"bbbb"
    )
    removed = store.register_artifact(
        run_id, "evidence/c.png", ArtifactType.SCREENSHOT, content=b"c"
    )
    store.finish_run(run_id, RunStatus.COMPLETED)
    run_dir = store.run_dir(run_id)
    assert verify_artifacts(run_dir, store.list_artifacts(run_id)).valid

    (run_dir / "evidence" / "b.png").write_bytes(b"bbbB")
    (run_dir / "evidence" / "c.png").unlink()
    before = snapshot(store.database_path)

    result = verify_artifacts(run_dir, store.list_artifacts(run_id))

    assert not result.valid
    assert result.checked == 3
    assert [(item.relative_path, item.kind) for item in result.problems] == [
        ("evidence/b.png", ProblemKind.MODIFIED),
        ("evidence/c.png", ProblemKind.MISSING),
    ]
    assert result.problems[0].message == (
        f"Artifact evidence/b.png was modified after the run (expected {tampered.sha256})"
    )
    assert removed.sha256 in result.problems[1].message
    assert kept.relative_path not in {item.relative_path for item in result.problems}
    assert snapshot(store.database_path) == before
    assert (run_dir / "evidence" / "b.png").read_bytes() == b"bbbB"
    assert store.get_run(run_id).status == RunStatus.COMPLETED


def test_verify_rejects_paths_outside_the_run(tmp_path: Path) -> None:
    (tmp_path / "outside.txt").write_text("x", encoding="utf-8")
    record = ArtifactRecord(
        id="art_x",
        run_id="run",
        relative_path="../outside.txt",
        artifact_type=ArtifactType.REPORT,
        size_bytes=1,
        sha256="0" * 64,
        created_at="x",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    result = verify_artifacts(run_dir, [record])

    assert [item.kind for item in result.problems] == [ProblemKind.UNSAFE]


def test_size_change_with_same_prefix_is_modified(history_context: ProjectContext) -> None:
    store = open_history(history_context)
    run_id = store.create_run(history_context, TextMission("Explore")).record.id
    store.register_artifact(run_id, "evidence/a.png", ArtifactType.SCREENSHOT, content=b"abc")
    (store.run_dir(run_id) / "evidence" / "a.png").write_bytes(b"abcd")

    result = verify_artifacts(store.run_dir(run_id), store.list_artifacts(run_id))

    assert [item.kind for item in result.problems] == [ProblemKind.MODIFIED]
