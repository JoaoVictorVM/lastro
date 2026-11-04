from typing import Any

import pytest

from lastro.change_context import ComparisonMode, PreparedContext
from lastro.change_context.collector import Collector, CollectRequest
from lastro.change_context.scanner import Gitleaks
from lastro.config.loader import ProjectContext
from lastro.config.source import SourceSettings
from lastro.history import MissionPlanError, RunStatus, TextMission, open_history

from .conftest import GitRepo


def _run_with_context(repo: GitRepo, context: ProjectContext) -> tuple[Any, Any, Any]:
    settings = SourceSettings(allow_paths=["src/**"], base_ref="main")
    draft = Collector(settings, repo.root, scanner=Gitleaks()).collect(
        CollectRequest(ComparisonMode.WORKING_TREE, flow_key="cadastro")
    )
    history = open_history(context)
    created = history.create_run(
        context, TextMission("Explore cadastro"), prepared=PreparedContext(change=draft)
    )
    return history, created.record, draft


def test_future_code_consumer_only_receives_controlled_snapshot_files(
    git_repo: GitRepo, gitleaks: str, history_context: ProjectContext
) -> None:
    git_repo.write(".env", "SECRET=1\n")
    git_repo.write("src/form.py", "def save(email=None):\n    return email\n")
    history, run, draft = _run_with_context(git_repo, history_context)
    context = history.get_change_context(run.id)
    assert context is not None
    approved = {source.id for source in context.sources}
    assert approved == {source.file_id for source in draft.sources}

    for file_id in approved:
        record, text = history.read_source_file(run.id, file_id)
        assert record.content_sha256 == next(
            source.content_sha256 for source in draft.sources if source.file_id == file_id
        )
        assert text
    with pytest.raises(MissionPlanError, match="invalid_reference"):
        history.read_source_file(run.id, "file_not_in_manifest")
    history.finish_run(run.id, RunStatus.COMPLETED)
    other = history.create_run(history_context, TextMission("Outra run")).record
    with pytest.raises(MissionPlanError, match="invalid_reference"):
        history.read_source_file(other.id, next(iter(approved)))

    artifact = next(
        item
        for item in history.list_artifacts(run.id)
        if item.relative_path.startswith("context/sources/")
    )
    path = history.run_dir(run.id) / artifact.relative_path
    path.chmod(0o644)
    path.write_text("tampered\n", encoding="utf-8")
    tampered = next(source.id for source in context.sources if source.artifact_id == artifact.id)
    with pytest.raises(MissionPlanError, match="context_integrity"):
        history.read_source_file(run.id, tampered)


def test_source_change_after_capture_does_not_change_context(
    git_repo: GitRepo, gitleaks: str, history_context: ProjectContext
) -> None:
    git_repo.write("src/form.py", "VERSION = 'captured'\n")
    history, run, _ = _run_with_context(git_repo, history_context)
    context = history.get_change_context(run.id)
    assert context is not None
    file_id = next(item.id for item in context.sources if item.relative_path == "src/form.py")

    git_repo.write("src/form.py", "VERSION = 'changed later'\n")
    (git_repo.root / "src" / "form.py").unlink()

    _, text = history.read_source_file(run.id, file_id)
    assert text == "VERSION = 'captured'\n"
    assert history.get_change_context(run.id) == context
