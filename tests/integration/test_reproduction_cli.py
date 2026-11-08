import pytest
from typer.testing import CliRunner

from lastro.cli.app import app
from lastro.config.loader import ProjectContext
from lastro.config.reproduction import ReproductionSettings
from lastro.history import HistoryStore, TextMission, open_history
from lastro.reproduction import coordinator as coordinator_module

from .reproduction_support import DirectLauncher, suspected

PROFILE = ["--profile", "homolog"]


@pytest.fixture
def launcher(monkeypatch: pytest.MonkeyPatch) -> DirectLauncher:
    fake = DirectLauncher()
    monkeypatch.setattr(
        coordinator_module, "default_launchers", lambda write: {"claude-code": fake}
    )
    return fake


@pytest.fixture
def store(history_context: ProjectContext) -> HistoryStore:
    return open_history(history_context)


def invoke(runner: CliRunner, *arguments: str) -> tuple[int, str]:
    result = runner.invoke(app, [*PROFILE, "run", "reproduce", *arguments])
    return result.exit_code, result.output


def test_active_run_is_refused(
    runner: CliRunner,
    store: HistoryStore,
    history_context: ProjectContext,
    launcher: DirectLauncher,
) -> None:
    run_id = store.create_run(history_context, TextMission("Explore")).record.id
    code, output = invoke(runner, run_id)
    assert code != 0
    assert f"Run {run_id} is still running." in output
    assert launcher.calls == []


def test_without_eligible_findings_exits_zero(
    runner: CliRunner,
    store: HistoryStore,
    history_context: ProjectContext,
    launcher: DirectLauncher,
) -> None:
    run_id = suspected(store, history_context, ())
    code, output = invoke(runner, run_id)
    assert code == 0, output
    assert "No suspected findings to reproduce." in output


def test_finding_option_and_idempotency(
    runner: CliRunner,
    store: HistoryStore,
    history_context: ProjectContext,
    launcher: DirectLauncher,
) -> None:
    run_id = suspected(store, history_context, ("Primeiro", "Segundo"))
    first, second = store.list_findings(run_id)

    code, output = invoke(runner, run_id, "--finding", second.id)
    assert code == 0, output
    assert "Reproducing 1 suspected finding…" in output
    assert "✓ confirmed (2/2)  Segundo" in output
    assert "Report:" in output
    assert store.get_reproduction_verdict(first.id) is None
    assert len(launcher.calls) == 2

    code, output = invoke(runner, run_id, "--finding", second.id)
    assert code != 0 and "reproduction_not_eligible" in output

    code, output = invoke(runner, run_id)
    assert code == 0, output
    assert "Reproducing 1 suspected finding…" in output
    code, output = invoke(runner, run_id)
    assert code == 0 and "No suspected findings to reproduce." in output
    assert len(launcher.calls) == 4


def test_verifier_option_requires_cross_mode(
    runner: CliRunner,
    store: HistoryStore,
    history_context: ProjectContext,
    launcher: DirectLauncher,
) -> None:
    run_id = suspected(store, history_context, ("Primeiro",))
    code, output = invoke(runner, run_id, "--verifier", "codex")
    assert code != 0
    assert "reproduction_config_invalid" in output
    assert launcher.calls == []


def test_auto_respects_configuration(
    runner: CliRunner,
    store: HistoryStore,
    history_context: ProjectContext,
    launcher: DirectLauncher,
) -> None:
    history_context.profile.reproduction = ReproductionSettings(auto=False)
    run_id = suspected(store, history_context, ("Primeiro",))
    code, output = invoke(runner, run_id, "--auto")
    assert code == 0, output
    assert launcher.calls == []
    code, output = invoke(runner, run_id)
    assert code == 0 and launcher.calls
