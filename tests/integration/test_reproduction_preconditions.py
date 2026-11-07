from collections.abc import Callable
from typing import Any

import pytest

from lastro.history import VerdictReason, open_history
from lastro.reproduction import trigger
from lastro.reproduction.coordinator import ReproductionCoordinator
from lastro_demo.flags import FlagState

from .reproduction_support import (
    PRECONDITION,
    ScriptedLauncher,
    configure,
    explore,
    missing_precondition,
)


@pytest.fixture
def demo_flags() -> FlagState:
    return FlagState.only("d02")


@pytest.fixture(autouse=True)
def no_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trigger, "SPAWN", lambda project, run_id: None)


def test_impossible_precondition_names_the_brief_item(
    make_auth_project: Callable[..., Any],
) -> None:
    context = make_auth_project().context()
    configure(context)
    history = open_history(context)
    run_id = explore(history, context)
    launcher = ScriptedLauncher("claude-code", missing_precondition)
    lines: list[str] = []
    ReproductionCoordinator(
        context, history, launchers={"claude-code": launcher}, write=lines.append
    ).run(run_id)

    finding = history.list_findings(run_id)[0]
    verdict = history.get_reproduction_verdict(finding.id)
    assert verdict is not None
    assert verdict.reason == VerdictReason.PRECONDITIONS
    assert verdict.missing_precondition == PRECONDITION
    assert len(launcher.attempts) == 1
    assert lines[-1].startswith(f"? inconclusive (preconditions): {PRECONDITION}")
