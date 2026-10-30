import errno
import hashlib
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

from lastro.config.loader import ProjectContext
from lastro.history import (
    ArtifactExistsError,
    ArtifactType,
    DiskFullError,
    FileMission,
    HistoryStore,
    RunStatus,
    TextMission,
    UnsafeArtifactPathError,
    open_history,
)
from lastro.history import layout as layout_module
from lastro.history.artifacts import normalize_relative_path


@pytest.fixture
def store(history_context: ProjectContext, fake_clock: Callable[[], datetime]) -> HistoryStore:
    return open_history(history_context, clock=fake_clock)


@pytest.fixture
def run_id(store: HistoryStore, history_context: ProjectContext) -> str:
    return store.create_run(history_context, TextMission("Explore o cadastro")).record.id


def test_artifact_row_matches_file_at_creation(store: HistoryStore, run_id: str) -> None:
    content = b"\x89PNG conteudo conhecido" * 1000

    record = store.register_artifact(
        run_id, "evidence/step-01.png", ArtifactType.SCREENSHOT, content=content
    )

    path = store.run_paths(run_id).run_dir / "evidence" / "step-01.png"
    assert path.read_bytes() == content
    assert record.size_bytes == path.stat().st_size == len(content)
    assert record.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert store.list_artifacts(run_id) == [record]


def test_artifact_can_be_copied_from_source(
    store: HistoryStore, run_id: str, tmp_path: Path
) -> None:
    source = tmp_path / "video.webm"
    source.write_bytes(b"video" * 4096)

    record = store.register_artifact(run_id, "evidence/run.webm", "video", source=source)

    assert record.artifact_type == ArtifactType.VIDEO
    assert record.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "relative",
    [
        "",
        "/etc/passwd",
        "C:/Windows/x.png",
        "..",
        "../fora.png",
        "evidence/../../fora.png",
        "evidence/./a.png",
        "evidence//a.png",
        "evidence\\a.png",
        " evidence/a.png",
    ],
)
def test_unsafe_paths_are_rejected_before_writing(
    store: HistoryStore, run_id: str, relative: str
) -> None:
    run_dir = store.run_paths(run_id).run_dir
    before = sorted(run_dir.parent.parent.rglob("*"))

    with pytest.raises(UnsafeArtifactPathError):
        store.register_artifact(run_id, relative, ArtifactType.SCREENSHOT, content=b"x")

    assert sorted(run_dir.parent.parent.rglob("*")) == before
    assert store.list_artifacts(run_id) == []


def test_unknown_type_is_rejected(store: HistoryStore, run_id: str) -> None:
    with pytest.raises(UnsafeArtifactPathError):
        store.register_artifact(run_id, "evidence/a.txt", "planilha", content=b"x")


def test_symlink_escape_is_rejected(store: HistoryStore, run_id: str, tmp_path: Path) -> None:
    outside = tmp_path / "fora"
    outside.mkdir()
    link = store.run_paths(run_id).run_dir / "evidence" / "link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError:
        pytest.skip("criação de symlink não permitida neste ambiente")

    with pytest.raises(UnsafeArtifactPathError):
        store.register_artifact(run_id, "evidence/link/a.png", "screenshot", content=b"x")

    assert list(outside.iterdir()) == []


def test_existing_artifact_is_never_overwritten(store: HistoryStore, run_id: str) -> None:
    store.register_artifact(run_id, "evidence/a.png", "screenshot", content=b"original")

    with pytest.raises(ArtifactExistsError):
        store.register_artifact(run_id, "evidence/a.png", "screenshot", content=b"outro")

    assert (store.run_paths(run_id).evidence_dir / "a.png").read_bytes() == b"original"


def test_disk_full_removes_temporary_and_blocks_run(
    store: HistoryStore, run_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full_disk(fd: int) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(layout_module.os, "fsync", full_disk)

    with pytest.raises(DiskFullError) as info:
        store.register_artifact(run_id, "evidence/big.png", "screenshot", content=b"x" * 10)

    monkeypatch.undo()
    evidence = store.run_paths(run_id).evidence_dir
    assert list(evidence.iterdir()) == []
    assert "evidence/big.png" in str(info.value)
    assert "x" * 10 not in str(info.value)
    record = store.get_run(run_id)
    assert record.status == RunStatus.BLOCKED
    assert record.status_reason == "Disco cheio em evidence/big.png; a evidência não pôde ser salva"
    assert store.list_artifacts(run_id) == []


def test_mission_snapshot_is_registered_as_snapshot_artifact(
    store: HistoryStore, history_context: ProjectContext, tmp_path: Path
) -> None:
    mission = tmp_path / "missao.md"
    mission.write_text("# Missão\nCadastrar funcionário\n", encoding="utf-8")

    created = store.create_run(history_context, FileMission(mission))

    (artifact,) = store.list_artifacts(created.record.id)
    assert artifact.artifact_type == ArtifactType.SNAPSHOT
    assert artifact.relative_path == "mission/source.md"
    assert artifact.sha256 == created.record.mission_file_sha256


def test_normalized_path_is_kept() -> None:
    assert normalize_relative_path("evidence/step-01.png") == "evidence/step-01.png"
