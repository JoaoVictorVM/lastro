import shutil
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from lastro.change_context import ComparisonMode, PreparedContext
from lastro.change_context.collector import Collector, CollectRequest
from lastro.change_context.scanner import Gitleaks
from lastro.cli.app import app
from lastro.config.loader import ProjectContext
from lastro.config.source import SourceSettings
from lastro.history import HistoryStore, IssueMission, RunStatus, open_history
from lastro.reporting import ReportBuilder
from lastro.reporting.public import export_public

from .conftest import GitRepo, make_issue_capture


def _remove(root: Path) -> None:
    # Objetos do Git são somente leitura no Windows; a remoção precisa liberar a escrita.
    shutil.rmtree(root, onexc=lambda func, path, _: (Path(path).chmod(0o700), func(path)))


def _draft(repo: GitRepo, **settings: Any) -> Any:
    config = SourceSettings(allow_paths=["src/**"], base_ref="main", **settings)
    return Collector(config, repo.root, scanner=Gitleaks()).collect(
        CollectRequest(ComparisonMode.FEATURE, flow_key="cadastro")
    )


def _run(context: ProjectContext, draft: Any, body: str = "Critério: email opcional.") -> Any:
    history = open_history(context)
    created = history.create_run(
        context,
        IssueMission(issue_snapshot_id="issue_report"),
        prepared=PreparedContext(issue=make_issue_capture(body=body), change=draft),
    )
    return history, created.record


def _report(history: HistoryStore, run_id: str) -> str:
    history.finish_run(run_id, RunStatus.COMPLETED)
    ReportBuilder(history).build_final(run_id)
    return (history.run_dir(run_id) / "report.md").read_text(encoding="utf-8")


def test_report_distinguishes_analyzed_and_executed_versions(
    git_repo: GitRepo, gitleaks: str, history_context: ProjectContext
) -> None:
    git_repo.write("src/form.py", "def save(email=None):\n    return email\n")
    head = git_repo.commit("email opcional")
    cases = (("v1.4.0", "unknown"), ("f" * 40, "mismatched"), (head, "matched"))
    history, run = None, None
    for executed, expected in cases:
        history, run = _run(history_context, _draft(git_repo))
        observation = history.observe_executed_version(
            run.id, executed, method="profile_version_endpoint", origin="/version"
        )
        assert observation["version_match"] == expected
        text = _report(history, run.id)
        assert (
            f"- Analyzed vs. executed version: `{head}` (analyzed code) vs. `{executed}` "
            rf"(executed system, profile\_version\_endpoint): `{expected}`"
        ) in text
        assert f"| Analyzed code version | {head} |" in text
        assert f"| Executed system version | {executed} |" in text
        assert ("not proven to be the executed system" in text) is (expected != "matched")
    assert history is not None and run is not None
    # Runs terminais são imutáveis: nenhuma observação posterior altera as versões.
    with pytest.raises(Exception, match="terminal"):
        history.observe_executed_version(run.id, head, method="x", origin="/")


def test_missing_executed_version_is_unknown(
    git_repo: GitRepo, gitleaks: str, history_context: ProjectContext
) -> None:
    history, run = _run(history_context, _draft(git_repo))
    text = _report(history, run.id)
    assert "(executed system, not observed): `unknown`" in text


def test_report_works_after_source_repo_removed(
    git_repo: GitRepo, gitleaks: str, history_context: ProjectContext
) -> None:
    git_repo.write("src/form.py", "x = 1\n")
    git_repo.commit("change")
    history, run = _run(history_context, _draft(git_repo))
    shutil.rmtree(
        git_repo.root, onerror=lambda func, path, _: (Path(path).chmod(0o700), func(path))
    )
    text = _report(history, run.id)
    assert "## Change context" in text
    assert r"- Issue: org/repo\#123" in text
    assert "comparison `feature`" in text


def test_exclusions_and_truncation_are_visible(
    git_repo: GitRepo, gitleaks: str, history_context: ProjectContext
) -> None:
    git_repo.write("src/a.py", "a = 1\n")
    git_repo.write("src/b.py", "b = 1\n")
    git_repo.write("src/secrets.yaml", "x: 1\n")
    git_repo.commit("files")
    history, run = _run(history_context, _draft(git_repo, max_candidates=1))
    text = _report(history, run.id)
    assert r"- Excluded: src/secrets.yaml: denied\_pattern" in text
    assert "- Partial result: limits reached; coverage is not complete." in text
    assert "local collection only" in text


def test_public_export_omits_private_change_context(
    git_repo: GitRepo, gitleaks: str, history_context: ProjectContext
) -> None:
    git_repo.write("src/form.py", "PRIVATE_MARKER = 1\n")
    head = git_repo.commit("change")
    history, run = _run(history_context, _draft(git_repo), body="Corpo privado da issue.")
    history.observe_executed_version(run.id, head, method="profile_version_endpoint", origin="/v")
    _report(history, run.id)

    exported = export_public(history, run.id).path.read_text(encoding="utf-8")

    for private in (head, "src/form.py", "Corpo privado", "org/repo", str(run.issue_snapshot_id)):
        assert private not in exported, private
    assert "## Change context" not in exported


def test_modified_snapshot_fails_report_verify(
    runner: CliRunner, git_repo: GitRepo, gitleaks: str, history_context: ProjectContext
) -> None:
    git_repo.write("src/form.py", "x = 1\n")
    git_repo.commit("change")
    history, run = _run(history_context, _draft(git_repo))
    _report(history, run.id)
    assert runner.invoke(app, ["report", "verify", run.id]).exit_code == 0

    summary = history.run_dir(run.id) / "context" / "summary.md"
    summary.write_text("tampered", encoding="utf-8")
    result = runner.invoke(app, ["report", "verify", run.id])

    assert result.exit_code == 2
    assert "Artifact context/summary.md was modified after the run" in result.stderr


def test_backup_restores_change_context_without_source_repo(
    git_repo: GitRepo, gitleaks: str, history_context: ProjectContext, tmp_path: Path
) -> None:
    from lastro.history import verify_restored_backup

    git_repo.write("src/form.py", "RESTORED = 1\n")
    git_repo.commit("restored marker")
    history, run = _run(history_context, _draft(git_repo))
    context = history.get_change_context(run.id)
    assert context is not None
    history.finish_run(run.id, RunStatus.COMPLETED)
    destination = tmp_path / "backups"
    destination.mkdir()
    archive = history.backup(destination).archive
    shutil.rmtree(
        git_repo.root, onerror=lambda func, path, _: (Path(path).chmod(0o700), func(path))
    )

    restored = tmp_path / "restored"
    with zipfile.ZipFile(archive) as opened:
        opened.extractall(restored)
    assert verify_restored_backup(restored) == []
    project = restored / history.project_id
    with sqlite3.connect(project / "history.sqlite") as conn:
        rows = conn.execute("SELECT id, artifact_id FROM source_files").fetchall()
        snapshots = conn.execute("SELECT COUNT(*) FROM issue_snapshots").fetchone()[0]
    assert {row[0] for row in rows} == {source.id for source in context.sources}
    assert snapshots == 1
    sources = list((project / "runs" / run.id / "context" / "sources").iterdir())
    assert any(path.read_text(encoding="utf-8") == "RESTORED = 1\n" for path in sources)
