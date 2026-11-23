from pathlib import Path
from typing import Any

import pytest

from lastro.autonomous.comparison import to_view
from lastro.autonomous.models import NO_BASELINE_MESSAGE, MatchLabel
from lastro.autonomous.notifications import build_message
from lastro.autonomous.runner import AutonomousPorts, ItemOutcome
from lastro.config.loader import ProjectContext
from lastro.history import (
    HistoryStore,
    ReportConflictError,
    ReportKind,
    RunStatus,
    SnapshotStatus,
    open_history,
)
from lastro.reporting.builder import ReportBuilder

from .autonomous_support import (
    FakeExecutor,
    RecordingAdapter,
    Script,
    coordinator,
    explore,
    found,
    manual,
    with_channels,
)


def _cycle(
    context: ProjectContext,
    history: HistoryStore,
    script: Script,
    key: str,
    *,
    adapters: dict[str, Any] | None = None,
    **fake: Any,
) -> ItemOutcome:
    history.autonomous.enqueue(manual(key))
    worker = coordinator(context, history, FakeExecutor(script=script, **fake))
    if adapters is not None:
        worker.ports = AutonomousPorts(
            **{**worker.ports.__dict__, "outbox": {"adapters": adapters, "environ": {}}}
        )
    (outcome,) = worker.drain("homolog")
    return outcome


@pytest.fixture
def history(history_context: ProjectContext) -> HistoryStore:
    return open_history(history_context)


def _labels(history: HistoryStore, run_id: str) -> dict[str, list[str]]:
    stored = history.autonomous.comparison_for_run(run_id)
    assert stored is not None
    labels: dict[str, list[str]] = {}
    for record in stored.result.matches:
        labels.setdefault(record.label.value, []).append(
            record.current_finding_id or record.previous_finding_id or ""
        )
    return labels


def test_comparison_precedes_full_findings(
    history_context: ProjectContext, history: HistoryStore
) -> None:
    _cycle(history_context, history, explore([found()]), "manual:1")
    other = found(title="Telefone some", flow="employee.edit", steps=("Editar", "Salvar"))
    second = _cycle(history_context, history, explore([found(), other]), "manual:2")
    assert second.run is not None and second.report_path is not None
    text = second.report_path.read_text(encoding="utf-8")
    assert second.report_path.name == "report.autonomous.md"
    assert text.startswith(f"# Lastro report: {second.run.id} (autonomous)")
    comparison = text.index("## Comparison with the previous run")
    assert (
        comparison
        < text.index("## Summary")
        < text.index(
            "CPF inválido é aceito", comparison + 1 + text[comparison:].index("## Summary")
        )
    )
    assert "New: 1 · Regressed: 0 · Recurring: 1 · Resolved: 0" in text
    labels = _labels(history, second.run.id)
    assert len(labels["recurring"]) == 1 and len(labels["new"]) == 1


def test_autonomous_report_is_immutable(
    history_context: ProjectContext, history: HistoryStore
) -> None:
    outcome = _cycle(history_context, history, explore([found()]), "manual:1")
    assert outcome.run is not None
    run_id = outcome.run.id
    final = history.list_report_snapshots(run_id, ReportKind.FINAL)
    autonomous = history.list_report_snapshots(run_id, ReportKind.AUTONOMOUS)
    assert [item.status for item in final] == [SnapshotStatus.READY]
    assert [item.status for item in autonomous] == [SnapshotStatus.READY]
    stored = history.autonomous.comparison_for_run(run_id)
    assert stored is not None
    view = to_view(history, stored.result, "manual")
    builder = ReportBuilder(history)
    again = builder.build_autonomous(run_id, view)
    assert again.snapshot.id == autonomous[0].id
    changed = to_view(history, stored.result, "schedule")
    with pytest.raises(ReportConflictError):
        builder.build_autonomous(run_id, changed)
    final_path = history.run_dir(run_id) / "report.md"
    assert "Comparison with the previous run" not in final_path.read_text(encoding="utf-8")


def test_no_baseline_message(history_context: ProjectContext, history: HistoryStore) -> None:
    outcome = _cycle(history_context, history, explore([found()]), "manual:1")
    assert outcome.run is not None and outcome.report_path is not None
    assert NO_BASELINE_MESSAGE in outcome.report_path.read_text(encoding="utf-8")
    assert list(_labels(history, outcome.run.id)) == ["new"]


def test_partial_flow_never_marks_resolved(
    history_context: ProjectContext, history: HistoryStore
) -> None:
    _cycle(history_context, history, explore([found()]), "manual:1")
    unrelated = found(title="Relatório vazio", flow="reports", steps=("Abrir relatórios",))
    partial = _cycle(history_context, history, explore([unrelated], evaluate=False), "manual:2")
    assert partial.run is not None
    second = _labels(history, partial.run.id)
    assert "resolved" not in second and len(second["not_comparable"]) == 1
    blocked = _cycle(history_context, history, explore([]), "manual:3", reachable=False)
    assert blocked.run is not None and blocked.run.status == RunStatus.BLOCKED
    third = _labels(history, blocked.run.id)
    assert "resolved" not in third
    whole = _cycle(history_context, history, explore([]), "manual:4")
    assert whole.run is not None
    assert list(_labels(history, whole.run.id)) == ["resolved"]
    regressed = _cycle(history_context, history, explore([found()]), "manual:5")
    assert regressed.run is not None
    assert list(_labels(history, regressed.run.id)) == [MatchLabel.REGRESSED.value]


def test_notification_links_builder_report(
    history_context: ProjectContext, history: HistoryStore
) -> None:
    context = with_channels(history_context)
    slack = RecordingAdapter()
    outcome = _cycle(
        context,
        history,
        explore([found()]),
        "manual:1",
        adapters={"slack": slack, "teams": RecordingAdapter(), "smtp": RecordingAdapter()},
    )
    assert outcome.run is not None and outcome.report_path is not None
    (message,) = slack.sent
    expected = outcome.report_path.relative_to(history.project_dir).as_posix()
    assert message.report == expected
    assert expected.endswith("report.autonomous.md")
    assert message.counts["new"] == 1
    assert message.status == "completed" and message.termination_reason is None


def test_fallback_is_notified(
    history_context: ProjectContext, history: HistoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = ReportBuilder.render_report

    def broken(self: ReportBuilder, data: Any) -> str:
        if data.kind == "autonomous":
            raise RuntimeError("template failure")
        return original(self, data)

    monkeypatch.setattr(ReportBuilder, "render_report", broken)
    context = with_channels(history_context)
    slack = RecordingAdapter()
    outcome = _cycle(
        context,
        history,
        explore([found()]),
        "manual:1",
        adapters={"slack": slack, "teams": RecordingAdapter(), "smtp": RecordingAdapter()},
    )
    assert outcome.report_path is not None
    assert outcome.report_path.name == "report.autonomous.json"
    assert slack.sent[0].report.endswith("report.autonomous.json")
    assert outcome.run is not None and outcome.run.status == RunStatus.COMPLETED


def test_message_reports_pending_items_and_reason(
    history_context: ProjectContext, history: HistoryStore
) -> None:
    outcome = _cycle(history_context, history, explore([]), "manual:1", reachable=False)
    assert outcome.run is not None
    message = build_message(history, outcome.item, outcome.run, None, Path("x"))
    assert message.status == "blocked" and message.termination_reason == "unreachable"
    assert any("blocked (unreachable)" in line for line in message.lines())
