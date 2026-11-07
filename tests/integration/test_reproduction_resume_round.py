from collections.abc import Callable
from typing import Any

import pytest

from lastro.history import FindingClassification, open_history
from lastro.reproduction import trigger
from lastro.reproduction.coordinator import ReproductionCoordinator
from lastro_demo.flags import FlagState

from .reproduction_support import ScriptedLauncher, configure, explore, replay_phone


@pytest.fixture
def demo_flags() -> FlagState:
    return FlagState.only("d02")


@pytest.fixture(autouse=True)
def no_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trigger, "SPAWN", lambda project, run_id: None)


def test_crash_between_attempts_resumes_without_repeating(
    make_auth_project: Callable[..., Any],
) -> None:
    context = make_auth_project().context()
    configure(context)
    history = open_history(context)
    run_id = explore(history, context)
    launcher = ScriptedLauncher("claude-code", replay_phone, fail_before=2)
    coordinator = ReproductionCoordinator(context, history, launchers={"claude-code": launcher})

    with pytest.raises(KeyboardInterrupt):
        coordinator.run(run_id)

    finding = history.list_findings(run_id)[0]
    assert finding.classification == FindingClassification.SUSPECTED
    assert len(finding.reproduction_attempts) == 1
    first_child = finding.reproduction_attempts[0].child_run_id
    status = history.reproduction_status(run_id)
    assert status.open_round is not None

    outcome = ReproductionCoordinator(context, history, launchers={"claude-code": launcher}).run(
        run_id
    )

    assert outcome.round_id == status.open_round.id
    assert len(launcher.attempts) == 2
    finding = history.get_finding(finding.id)
    assert [item.ordinal for item in finding.reproduction_attempts] == [1, 2]
    assert finding.reproduction_attempts[0].child_run_id == first_child
    verdict = history.get_reproduction_verdict(finding.id)
    assert verdict is not None and verdict.label == "confirmed (2/2)"
    assert history.reproduction_status(run_id).open_round is None
