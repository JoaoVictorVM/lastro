import asyncio
import unicodedata
from collections.abc import Callable
from typing import Any, cast

import pytest
from playwright.async_api import Page

from lastro.browser.tools import BrowserTools
from lastro.config.loader import ProjectContext
from lastro.history import FindingClassification, HistoryStore, TextMission, open_history
from lastro.reproduction import REPRODUCTION_TOOL, trigger
from lastro.reproduction.coordinator import ReproductionCoordinator
from lastro_demo.flags import FlagState

from .reproduction_support import Attempt, ScriptedLauncher, configure

LOADS_PER_ATTEMPT = 2


@pytest.fixture
def demo_flags() -> FlagState:
    return FlagState.only("d08")


@pytest.fixture(autouse=True)
def no_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trigger, "SPAWN", lambda project, run_id: None)


def fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()


async def unsorted_load(runtime: BrowserTools) -> bool:
    base = runtime.project.profile.target_url
    await runtime.call("navigate", {"url": base + "/employees?sort=name&dir=asc"})
    page = cast(Page, runtime.session.page)
    names = [fold(name) for name in await page.locator("td.name a").all_inner_texts()]
    return names != sorted(names)


async def replay_sorting(runtime: BrowserTools, attempt: Attempt) -> None:
    seen = False
    for _ in range(LOADS_PER_ATTEMPT):
        seen = await unsorted_load(runtime) or seen
    shot = await runtime.call("screenshot", {})
    if seen:
        await runtime.call(
            REPRODUCTION_TOOL,
            {"result": "reproduced", "evidence_artifact_ids": [shot["artifact_id"]]},
        )
    else:
        await runtime.call(REPRODUCTION_TOOL, {"result": "not_reproduced"})


def explore_sorting(history: HistoryStore, context: ProjectContext) -> str:
    run_id = history.create_run(context, TextMission("Explore a listagem")).record.id
    runtime = BrowserTools(context, history, run_id)

    async def scenario() -> None:
        try:
            await unsorted_load(runtime)
            shot = await runtime.call("screenshot", {})
            await runtime.call(
                "report_finding",
                {
                    "title": "Ordenação por nome às vezes ignorada",
                    "flow": "Listagem de funcionários",
                    "steps": ["Abrir a listagem ordenada por nome ascendente"],
                    "expected": {
                        "text": "A listagem fica em ordem alfabética",
                        "source": "common_convention",
                    },
                    "observed": "Em algumas cargas a ordem é a de inserção.",
                    "evidence": [{"artifact_id": shot["artifact_id"], "purpose": "state"}],
                    "classification": "suspected",
                    "failure_domain": "system",
                    "intermittent": True,
                },
            )
            await runtime.call("end_run", {"outcome": "completed"})
        finally:
            await runtime.session.close()

    asyncio.run(scenario())
    return run_id


def test_intermittent_defect_reports_observed_frequency(
    make_auth_project: Callable[..., Any],
) -> None:
    context = make_auth_project().context()
    configure(context)
    history = open_history(context)
    run_id = explore_sorting(history, context)
    finding = history.list_findings(run_id)[0]
    assert finding.intermittent

    launcher = ScriptedLauncher("claude-code", replay_sorting)
    ReproductionCoordinator(context, history, launchers={"claude-code": launcher}).run(run_id)

    verdict = history.get_reproduction_verdict(finding.id)
    assert verdict is not None
    # Marcado intermittent: 3 tentativas, e a frequência vem do que a reprodução observou.
    assert len(launcher.attempts) == 3
    assert verdict.attempts_planned == 3 and verdict.attempts_decisive == 3
    assert verdict.classification == FindingClassification.CONFIRMED
    assert 1 <= verdict.reproduced_count < 3
    assert verdict.intermittent
    assert verdict.label == f"confirmed ({verdict.reproduced_count}/3, intermittent)"
    assert history.get_finding(finding.id).classification == FindingClassification.CONFIRMED
