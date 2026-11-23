"""Missão autônoma ponta a ponta contra o demo, com o executor nativo e um provedor roteirizado.

Usa o pipeline real: trigger, fila, lease, preflight de alvo e login, run headless unattended no
browser, comparação, relatório autônomo e outbox. Nenhum segredo real: as credenciais são as do
seed do demo e a chave do provedor é fictícia. Com LASTRO_AUTONOMOUS_ARTIFACT_DIR definido, o
relatório e o resumo são copiados para lá, para o workflow publicá-los como artifact.
"""

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from lastro.cli.app import app
from lastro.history import ReportKind, RunStatus, SnapshotStatus, open_history
from lastro.native import runner as native_runner

from .conftest import AuthProject, DemoServer
from .test_cli_native import demo_script

ARTIFACT_ENV = "LASTRO_AUTONOMOUS_ARTIFACT_DIR"
AUTONOMOUS = """
[autonomous]
enabled = true

[[autonomous.notifications]]
name = "demo-teams"
type = "teams"
webhook_env = "LASTRO_DEMO_TEAMS_WEBHOOK"

"""
MISSION = "Confira a lista de funcionários da Clínica Aurora e registre o critério observado.\n"


def test_demo_autonomous_run_produces_report_and_summary(
    native_demo: AuthProject,
    scripted: Any,
    demo_server: DemoServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = scripted.Model(demo_script(scripted, demo_server.url))
    monkeypatch.setattr(native_runner, "PROVIDER_FACTORY", lambda key: model)
    monkeypatch.delenv("LASTRO_DEMO_TEAMS_WEBHOOK", raising=False)
    project_dir = native_demo.config_path.parent
    text = native_demo.config_path.read_text(encoding="utf-8")
    first = text.index("[profiles.")
    native_demo.config_path.write_text(text[:first] + AUTONOMOUS + text[first:], encoding="utf-8")
    (project_dir / "missions").mkdir(exist_ok=True)
    (project_dir / "missions" / "demo.md").write_text(MISSION, encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["--profile", "demo", "autonomous", "run", "--mission-file", "missions/demo.md", "--json"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout.strip().splitlines()[-1])
    assert (data["status"], data["run_status"]) == ("completed", "completed"), data
    history = open_history(native_demo.context())
    run = history.get_run(data["run_id"])
    assert run.status == RunStatus.COMPLETED
    assert run.config_snapshot["autonomous"]["gate_mode"] == "unattended"  # type: ignore[index]
    snapshots = history.list_report_snapshots(run.id, ReportKind.AUTONOMOUS)
    assert [item.status for item in snapshots] == [SnapshotStatus.READY]
    report = Path(data["report"])
    text = report.read_text(encoding="utf-8")
    assert text.index("## Comparison with the previous run") < text.index("## Summary")
    assert "No previous run to compare; all findings shown as new." in text
    (notification,) = history.autonomous.list_notifications(data["item_id"])
    lines = list(notification.message.lines())
    assert len(lines) <= 20
    assert lines[0] == "Lastro · demo · 0 new findings, 0 regressed, 0 recurring"
    assert lines[-1].endswith("report.autonomous.md")
    # Sem webhook real configurado, a entrega falha sem mudar a run.
    assert notification.last_error_code == "webhook_env_missing"

    target = os.environ.get(ARTIFACT_ENV)
    if target:
        destination = Path(target)
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(report, destination / "report.autonomous.md")
        final = history.run_dir(run.id) / "report.md"
        if final.is_file():
            shutil.copyfile(final, destination / "report.md")
        (destination / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
