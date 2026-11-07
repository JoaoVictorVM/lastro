from collections.abc import Callable
from typing import Any

import pytest

from lastro.config.reproduction import ReproductionSettings
from lastro.history import AttemptReason, RunStatus, VerdictReason, open_history
from lastro.reproduction import trigger
from lastro.reproduction.coordinator import ReproductionCoordinator
from lastro_demo.flags import FlagState

from .reproduction_support import ScriptedLauncher, busy, configure, explore


@pytest.fixture
def demo_flags() -> FlagState:
    return FlagState.only("d02")


@pytest.fixture(autouse=True)
def no_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trigger, "SPAWN", lambda project, run_id: None)


def test_child_exceeding_actions_is_inconclusive_budget(
    make_auth_project: Callable[..., Any],
) -> None:
    context = make_auth_project().context()
    configure(context)
    history = open_history(context)
    run_id = explore(history, context)
    context.profile.reproduction = ReproductionSettings(max_actions=4)

    launcher = ScriptedLauncher("claude-code", busy)
    ReproductionCoordinator(context, history, launchers={"claude-code": launcher}).run(run_id)

    assert len(launcher.attempts) == 2
    for attempt in launcher.attempts:
        child = history.get_run(attempt.run_id)
        assert (child.status, child.status_reason) == (RunStatus.LIMIT_REACHED, "action_limit")
        limits = child.config_snapshot["run_limits"]
        assert isinstance(limits, dict) and limits["max_actions"] == 4
    finding = history.list_findings(run_id)[0]
    assert [item.reason for item in finding.reproduction_attempts] == [AttemptReason.BUDGET] * 2
    verdict = history.get_reproduction_verdict(finding.id)
    assert verdict is not None and verdict.reason == VerdictReason.BUDGET
    assert verdict.label == "inconclusive (budget)"
