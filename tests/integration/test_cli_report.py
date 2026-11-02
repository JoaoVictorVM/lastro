from typer.testing import CliRunner

from lastro.cli.app import app
from lastro.config.loader import ProjectContext
from lastro.history import ArtifactType, HistoryStore, RunStatus, TextMission, open_history
from lastro.reporting import ReportBuilder


def finished(context: ProjectContext) -> tuple[HistoryStore, str]:
    store = open_history(context)
    run_id = store.create_run(context, TextMission("Explore")).record.id
    store.register_artifact(run_id, "evidence/a.png", ArtifactType.SCREENSHOT, content=b"aaaa")
    store.finish_run(run_id, RunStatus.COMPLETED)
    ReportBuilder(store).build_final(run_id)
    return store, run_id


def test_verify_tampered_evidence_exits_two(
    runner: CliRunner, history_context: ProjectContext
) -> None:
    store, run_id = finished(history_context)

    clean = runner.invoke(app, ["report", "verify", run_id])
    assert clean.exit_code == 0, clean.output
    assert f"artifacts of run {run_id} match their recorded hashes" in clean.stdout

    shot = store.run_dir(run_id) / "evidence" / "a.png"
    shot.write_bytes(b"aaab")
    expected = next(a for a in store.list_artifacts(run_id) if a.relative_path == "evidence/a.png")
    tampered = runner.invoke(app, ["report", "verify", run_id])

    assert tampered.exit_code == 2
    assert (
        f"Artifact evidence/a.png was modified after the run (expected {expected.sha256})"
        in tampered.stderr
    )
    assert shot.read_bytes() == b"aaab"
    assert store.get_run(run_id).status == RunStatus.COMPLETED


def test_verify_lists_every_invalid_file(
    runner: CliRunner, history_context: ProjectContext
) -> None:
    store, run_id = finished(history_context)
    (store.run_dir(run_id) / "evidence" / "a.png").unlink()
    (store.run_dir(run_id) / "report.md").write_text("edited", encoding="utf-8")

    result = runner.invoke(app, ["report", "verify", run_id])

    assert result.exit_code == 2
    assert "Artifact evidence/a.png is missing" in result.stderr
    assert "Artifact report.md was modified after the run" in result.stderr
    assert "2 of 2 artifacts" in result.stderr


def test_unknown_run_exits_one(runner: CliRunner, history_context: ProjectContext) -> None:
    open_history(history_context)
    for command in (["report", "verify", "run_x"], ["report", "export", "--public", "run_x"]):
        result = runner.invoke(app, command)
        assert result.exit_code == 1
        assert "Run not found in this project: run_x" in result.stderr


def test_export_public_prints_path(runner: CliRunner, history_context: ProjectContext) -> None:
    store, run_id = finished(history_context)

    result = runner.invoke(app, ["report", "export", "--public", run_id])

    assert result.exit_code == 0, result.output
    path = store.run_dir(run_id) / "report.public.md"
    assert str(path) in result.stdout
    assert history_context.profile.tenant_id not in path.read_text(encoding="utf-8")
    assert runner.invoke(app, ["report", "export", run_id]).exit_code == 1


def test_export_requires_ready_report(runner: CliRunner, history_context: ProjectContext) -> None:
    store = open_history(history_context)
    run_id = store.create_run(history_context, TextMission("Explore")).record.id

    result = runner.invoke(app, ["report", "export", "--public", run_id])

    assert result.exit_code == 1
    assert "has no ready report" in result.stderr
