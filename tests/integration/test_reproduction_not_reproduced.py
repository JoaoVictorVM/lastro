from collections.abc import Callable
from typing import Any

import pytest

from lastro.history import FindingClassification, ReproductionOutcome, VerdictReason, open_history
from lastro.reproduction import trigger
from lastro.reproduction.coordinator import ReproductionCoordinator
from lastro_demo.flags import FlagState

from .reproduction_support import ScriptedLauncher, configure, explore, replay_phone


@pytest.fixture
def demo_flags() -> FlagState:
    return FlagState.only()


@pytest.fixture(autouse=True)
def no_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trigger, "SPAWN", lambda project, run_id: None)


def test_fixed_defect_is_inconclusive_never_confirmed(
    make_auth_project: Callable[..., Any],
) -> None:
    context = make_auth_project().context()
    configure(context)
    history = open_history(context)
    run_id = explore(history, context)
    launcher = ScriptedLauncher("claude-code", replay_phone)
    lines: list[str] = []
    ReproductionCoordinator(
        context, history, launchers={"claude-code": launcher}, write=lines.append
    ).run(run_id)

    finding = history.list_findings(run_id)[0]
    verdict = history.get_reproduction_verdict(finding.id)
    assert verdict is not None
    assert verdict.classification == FindingClassification.INCONCLUSIVE
    assert verdict.reason == VerdictReason.NOT_REPRODUCED
    assert (verdict.reproduced_count, verdict.attempts_decisive) == (0, 2)
    assert finding.classification == FindingClassification.INCONCLUSIVE
    assert [item.outcome for item in finding.reproduction_attempts] == [
        ReproductionOutcome.NOT_REPRODUCED
    ] * 2
    assert lines[-1].startswith("✗ not reproduced")
