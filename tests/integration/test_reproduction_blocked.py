from collections.abc import Callable
from typing import Any

import pytest

from lastro.browser.demo import FIELDS
from lastro.browser.tools import BrowserTools
from lastro.history import (
    AttemptReason,
    FindingClassification,
    VerdictReason,
    open_history,
)
from lastro.reproduction import REPRODUCTION_TOOL, trigger
from lastro.reproduction.coordinator import ReproductionCoordinator
from lastro_demo.flags import FlagState

from .reproduction_support import (
    NEW_PHONE,
    PHONE,
    SAVE,
    Attempt,
    ScriptedLauncher,
    configure,
    explore,
)


@pytest.fixture
def demo_flags() -> FlagState:
    return FlagState.only("d02")


@pytest.fixture(autouse=True)
def no_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trigger, "SPAWN", lambda project, run_id: None)


async def assertive_replay(runtime: BrowserTools, attempt: Attempt) -> None:
    """Reprodutor que declara reprodução mesmo com a edição negada pela política."""
    base = runtime.project.profile.target_url
    await runtime.call("navigate", {"url": base + "/employees/1/edit"})
    await runtime.call("fill", {"target": PHONE, "value": NEW_PHONE})
    await runtime.call("submit", {"target": SAVE})
    shot = await runtime.call("screenshot", {})
    await runtime.call(
        REPRODUCTION_TOOL, {"result": "reproduced", "evidence_artifact_ids": [shot["artifact_id"]]}
    )


async def honest_replay(runtime: BrowserTools, attempt: Attempt) -> None:
    base = runtime.project.profile.target_url
    await runtime.call("navigate", {"url": base + "/employees/1/edit"})
    await runtime.call("fill", {"target": PHONE, "value": NEW_PHONE})
    await runtime.call("submit", {"target": SAVE})
    await runtime.call(REPRODUCTION_TOOL, {"result": "not_reproduced"})


@pytest.mark.parametrize("script", [honest_replay, assertive_replay])
def test_policy_block_is_inconclusive_blocked_not_not_reproduced(
    make_auth_project: Callable[..., Any], script: Any
) -> None:
    project = make_auth_project()
    explorer = project.context()
    configure(explorer)
    history = open_history(explorer)
    run_id = explore(history, explorer)

    # Perfil da filha mais restritivo: o telefone deixou de ser editável depois da exploração.
    restricted = project.context()
    configure(restricted, editable=tuple(name for name in FIELDS if name != "phone"))
    launcher = ScriptedLauncher("claude-code", script)
    ReproductionCoordinator(restricted, history, launchers={"claude-code": launcher}).run(run_id)

    finding = history.list_findings(run_id)[0]
    verdict = history.get_reproduction_verdict(finding.id)
    assert verdict is not None
    assert verdict.reason == VerdictReason.BLOCKED
    assert verdict.label == "inconclusive (blocked)"
    assert finding.classification == FindingClassification.INCONCLUSIVE
    # Bloqueio não muda ao repetir: a segunda tentativa não é lançada.
    assert len(launcher.attempts) == 1
    assert finding.reproduction_attempts[0].reason == AttemptReason.BLOCKED
