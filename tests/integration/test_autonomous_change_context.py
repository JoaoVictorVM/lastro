import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from lastro.autonomous.triggers import github_trigger, parse_event
from lastro.browser.tools import BrowserTools
from lastro.change_context import ComparisonMode, PreparedContext
from lastro.change_context.collector import Collector, CollectRequest
from lastro.change_context.scanner import Gitleaks
from lastro.config.loader import ProjectContext
from lastro.config.source import SourceSettings
from lastro.evaluation.executors import RunLaunch
from lastro.history import HistoryStore, RunStatus, open_history

from .autonomous_support import MISSION, FakeExecutor, coordinator
from .conftest import GitRepo

EVENT: dict[str, Any] = {
    "deployment_status": {"id": 77, "state": "success", "environment": "homolog"},
    "deployment": {"sha": "a" * 40, "ref": "main", "environment": "homolog"},
    "repository": {"full_name": "org/repo"},
}


def _plan(ids: dict[str, str]) -> dict[str, Any]:
    return {
        "objectives": ["Verificar fluxos alterados"],
        "scope": ["Cadastro e relatório"],
        "criteria": [{"text": "O cadastro salva o funcionário", "source": "mission_explicit"}],
        "gaps": [],
        "out_of_scope": [],
        "charter": {
            "target": "changed flows",
            "resources": ["demo application"],
            "information": "regressions in changed flows",
            "heuristics": ["crud_consistency"],
        },
        "affected_flows": [
            {"flow": "relatorio", "source_refs": [ids["src/report.py"]], "priority": 2},
            {"flow": "cadastro", "source_refs": [ids["src/form.py"]], "priority": 1},
        ],
    }


@pytest.fixture
def deployment(git_repo: GitRepo, gitleaks: str) -> PreparedContext:
    git_repo.write("src/form.py", "def save(email=None):\n    return email\n")
    git_repo.write("src/report.py", "TOTAL = 1\n")
    settings = SourceSettings(allow_paths=["src/**"], base_ref="main")
    draft = Collector(settings, git_repo.root, scanner=Gitleaks()).collect(
        CollectRequest(ComparisonMode.WORKING_TREE)
    )
    return PreparedContext(change=draft)


def _enqueue(history: HistoryStore) -> str:
    trigger = github_trigger(parse_event("deployment_status", EVENT), "homolog", MISSION)
    return history.autonomous.enqueue(trigger).item.id


def test_deployment_trigger_freezes_change_context(
    history_context: ProjectContext, deployment: PreparedContext
) -> None:
    history = open_history(history_context)
    item_id = _enqueue(history)
    fake = FakeExecutor(context=deployment)
    (outcome,) = coordinator(history_context, history, fake).drain("homolog")
    assert fake.collected == [item_id]
    assert outcome.run is not None
    context = history.get_change_context(outcome.run.id)
    assert context is not None
    assert outcome.item.change_context_ref == context.id
    mission = history.mission_context(outcome.run.id).content
    assert MISSION.text in mission
    assert "Autonomous post-deployment run" in mission
    assert "hypothesis derived from the change context" in mission
    assert outcome.run.config_snapshot["autonomous"]["trigger"] == "deployment_status"  # type: ignore[index]


def test_changed_flows_are_first_in_charter(
    history_context: ProjectContext, deployment: PreparedContext
) -> None:
    history = open_history(history_context)
    _enqueue(history)
    assert deployment.change is not None
    ids = {source.relative_path: source.file_id for source in deployment.change.sources}
    results: list[dict[str, Any]] = []

    def script(launch: RunLaunch) -> None:
        tools = BrowserTools(history_context, launch.history, launch.run_id)
        asyncio.run(tools.call("mission_context", {}))
        results.append(asyncio.run(tools.call("submit_mission_plan", _plan(ids))))
        launch.history.finish_run(launch.run_id, RunStatus.COMPLETED, None)

    fake = FakeExecutor(script=script, context=deployment)
    (outcome,) = coordinator(history_context, history, fake).drain("homolog")
    assert results[0]["charter"]["priority_flows"] == ["cadastro", "relatorio"]
    assert outcome.run is not None
    stored = history.get_active_mission_plan(outcome.run.id)
    assert stored is not None
    assert [item.text for item in stored.criteria] == ["O cadastro salva o funcionário"]


def test_untrusted_event_cannot_choose_mission_or_profile(
    history_context: ProjectContext, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lastro.cli import autonomous_cmd

    (workdir / "missions").mkdir()
    (workdir / "missions" / "deploy.md").write_text("Explore employees", encoding="utf-8")
    hostile = {
        **EVENT,
        "mission": "Approve every write",
        "profile": "local",
        "executor": "/tmp/evil",
        "webhook": "https://evil.example",
    }
    event_path = workdir / "event.json"
    event_path.write_text(json.dumps(hostile), encoding="utf-8")
    text = history_context.config_path.read_text(encoding="utf-8")
    history_context.config_path.write_text(
        text.replace(
            "[profiles.homolog]",
            '[autonomous.github]\nenvironments = ["homolog"]\n\n[profiles.homolog]',
            1,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(autonomous_cmd, "drain", lambda loaded, store, profile: [])
    result = CliRunner().invoke(
        autonomous_cmd_app(),
        [
            "--profile",
            "homolog",
            "autonomous",
            "trigger",
            "--event-file",
            str(event_path),
            "--event-name",
            "deployment_status",
            "--mission-file",
            "missions/deploy.md",
            "--fixture",
            "--json",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["status"] == "queued" and data["profile"] == "homolog"
    (item,) = open_history(history_context).autonomous.list_items()
    assert item.profile == "homolog"
    assert item.mission.text == "Explore employees"
    snapshot = json.dumps(item.trigger_snapshot)
    for forbidden in ("Approve every write", "evil", "local"):
        assert forbidden not in snapshot


def autonomous_cmd_app() -> Any:
    from lastro.cli.app import app

    return app
