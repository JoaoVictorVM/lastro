from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from lastro.config.reproduction import ReproductionSettings
from lastro.history import FindingClassification, VerdictReason, open_history
from lastro.reproduction import trigger
from lastro.reproduction.coordinator import ReproductionCoordinator
from lastro_demo.flags import FlagState

from .reproduction_support import ScriptedLauncher, configure, explore, replay_phone

CROSS = ReproductionSettings(mode="cross", verifiers={"claude-code": "codex"})


@pytest.fixture
def demo_flags() -> FlagState:
    return FlagState.only("d02")


@pytest.fixture(autouse=True)
def no_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trigger, "SPAWN", lambda project, run_id: None)


def test_verifier_differs_and_both_executors_are_recorded(
    make_auth_project: Callable[..., Any],
) -> None:
    context = make_auth_project().context()
    configure(context)
    context.profile.reproduction = CROSS
    history = open_history(context)
    run_id = explore(history, context)
    explorer = ScriptedLauncher("claude-code", replay_phone)
    verifier = ScriptedLauncher("codex", replay_phone)

    outcome = ReproductionCoordinator(
        context, history, launchers={"claude-code": explorer, "codex": verifier}
    ).run(run_id)

    assert explorer.attempts == [] and len(verifier.attempts) == 2
    for attempt in verifier.attempts:
        assert history.get_run(attempt.run_id).executor_name == "codex"
    finding = history.list_findings(run_id)[0]
    verdict = history.get_reproduction_verdict(finding.id)
    assert verdict is not None
    assert history.get_run(run_id).executor_name == "claude-code"
    assert (verdict.explorer_executor, verdict.verifier_executor) == ("claude-code", "codex")
    assert verdict.cross_executor
    assert finding.classification == FindingClassification.CONFIRMED
    assert {item.executor for item in finding.reproduction_attempts} == {"codex"}
    assert outcome.report_path is not None
    report = (history.run_dir(run_id) / "report.reproduction-v1.md").read_text(encoding="utf-8")
    assert "Explorer: claude-code; verifier: codex (cross-executor)" in report


def test_pair_equal_to_explorer_fails_at_load() -> None:
    with pytest.raises(ValidationError):
        ReproductionSettings(mode="cross", verifiers={"claude-code": "claude-code"})


def test_missing_verifier_is_inconclusive_executor_without_fallback(
    make_auth_project: Callable[..., Any],
) -> None:
    context = make_auth_project().context()
    configure(context)
    context.profile.reproduction = CROSS
    history = open_history(context)
    run_id = explore(history, context)
    same = ScriptedLauncher("claude-code", replay_phone)
    missing = ScriptedLauncher("codex", replay_phone, available=False)

    lines: list[str] = []
    ReproductionCoordinator(
        context, history, launchers={"claude-code": same, "codex": missing}, write=lines.append
    ).run(run_id)

    assert same.attempts == [] and missing.attempts == []
    finding = history.list_findings(run_id)[0]
    verdict = history.get_reproduction_verdict(finding.id)
    assert verdict is not None
    assert verdict.reason == VerdictReason.EXECUTOR
    assert verdict.verifier_executor == "codex" and verdict.cross_executor
    assert any("codex unavailable" in note for note in verdict.notes)
    assert lines[-1].startswith("? inconclusive (executor)")
