from collections.abc import Callable
from typing import Any

import pytest

from lastro.history import (
    EventType,
    FindingClassification,
    PolicyDecision,
    ReproductionOutcome,
    open_history,
)
from lastro.reproduction import trigger
from lastro.reproduction.coordinator import ReproductionCoordinator
from lastro_demo.flags import FlagState

from .reproduction_support import (
    OBSERVED,
    PRECONDITION,
    ScriptedLauncher,
    configure,
    explore,
    replay_phone,
)


@pytest.fixture
def demo_flags() -> FlagState:
    return FlagState.only("d02")


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(trigger, "SPAWN", lambda project, run_id: calls.append(run_id))
    return calls


def test_deterministic_defect_confirmed_in_two_child_runs(
    make_auth_project: Callable[..., Any], spawned: list[str]
) -> None:
    context = make_auth_project().context()
    configure(context)
    history = open_history(context)
    run_id = explore(history, context)
    # O owner do claude-code inicia a reprodução desacoplada ao encerrar uma run completed.
    assert spawned == [run_id]

    launcher = ScriptedLauncher("claude-code", replay_phone)
    lines: list[str] = []
    outcome = ReproductionCoordinator(
        context, history, launchers={"claude-code": launcher}, write=lines.append
    ).run(run_id)

    assert lines[0] == "Reproducing 1 suspected finding…"
    assert lines[1].startswith("✓ confirmed (2/2)")
    finding = history.list_findings(run_id)[0]
    assert finding.classification == FindingClassification.CONFIRMED
    verdict = history.get_reproduction_verdict(finding.id)
    assert verdict is not None and verdict.label == "confirmed (2/2)"
    assert (verdict.explorer_executor, verdict.verifier_executor) == ("claude-code", "claude-code")
    assert not verdict.cross_executor
    attempts = finding.reproduction_attempts
    assert [item.outcome for item in attempts] == [ReproductionOutcome.REPRODUCED] * 2
    children = [item.run_id for item in launcher.attempts]
    assert len(set(children)) == 2 and run_id not in children
    for attempt, child in zip(attempts, children, strict=True):
        assert attempt.child_run_id == child
        child_artifacts = {item.id for item in history.list_artifacts(child)}
        assert set(attempt.evidence_artifact_ids) <= child_artifacts

    # Cada tentativa gera dados novos com o marcador da própria run filha.
    markers = {item.marker for item in launcher.attempts}
    assert len(markers) == 2 and all(markers)
    # A missão da filha é o brief: passos e pré-condições, nunca o observado nem o finding.
    for attempt in launcher.attempts:
        assert PRECONDITION in attempt.mission
        assert "Salvar" in attempt.mission
        assert OBSERVED not in attempt.mission
        assert finding.id not in attempt.mission
        assert attempt.knowledge == {"available": False}

    # Mesmas tools e políticas: a edição da filha passou pela decisão de política normal.
    for child in children:
        decisions = [
            event
            for event in history.list_events(child)
            if event.event_type == EventType.POLICY_DECISION and event.tool_name == "submit"
        ]
        assert decisions and decisions[0].policy_decision == PolicyDecision.ALLOW

    events = [event.event_type for event in history.list_events(run_id)]
    assert events.count(EventType.REPRODUCTION_ATTEMPT_FINISHED) == 2
    assert events[-1] == EventType.REPRODUCTION_VERDICT
    assert outcome.report_path is not None and "reproduction-v1" in outcome.report_path
    report = (history.run_dir(run_id) / "report.reproduction-v1.md").read_text(encoding="utf-8")
    assert "Reproduction verdict:** confirmed (2/2)" in report
    assert "same executor" in report


def test_brief_and_finding_contract_survive_idempotent_rerun(
    make_auth_project: Callable[..., Any], spawned: list[str]
) -> None:
    context = make_auth_project().context()
    configure(context)
    history = open_history(context)
    run_id = explore(history, context)
    launcher = ScriptedLauncher("claude-code", replay_phone)
    coordinator = ReproductionCoordinator(context, history, launchers={"claude-code": launcher})
    coordinator.run(run_id)
    again = coordinator.run(run_id)
    assert again.message == "No suspected findings to reproduce."
    assert len(launcher.attempts) == 2
