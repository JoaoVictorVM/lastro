"""Explorador e reprodutor roteirizados contra o demo, pelo mesmo caminho de tools e políticas.

O reprodutor de teste é um lançador da porta real: a run filha é criada pelo coordenador e o
roteiro só usa `BrowserTools`, como um executor faria pelo MCP.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, cast

from playwright.async_api import Page

from lastro.browser.demo import FIELDS, demo_policy
from lastro.browser.tools import BrowserTools
from lastro.config.loader import ProjectContext
from lastro.config.policy import Preauthorized
from lastro.history import (
    ArtifactType,
    CreatedRun,
    EvidencePurpose,
    EvidenceReference,
    ExecutorInfo,
    ExpectedSource,
    FailureDomain,
    FindingClassification,
    FindingDraft,
    HistoryStore,
    ReproducerResult,
    RunStatus,
    TextMission,
)
from lastro.reproduction import REPRODUCTION_TOOL
from lastro.reproduction.launchers import (
    ExecutorUnavailable,
    LaunchOutcome,
    PreparedChild,
)

PHONE = {"label": "Telefone"}
SAVE = {"role": "button", "name": "Salvar"}
NEW_PHONE = "(11) 3456-7890"
OBSERVED = "O telefone volta ao valor anterior depois de recarregar a página de edição."
PRECONDITION = "Funcionário 1 existe no tenant aurora"

type Script = Callable[[BrowserTools, "Attempt"], Awaitable[None]]


def configure(context: ProjectContext, *, editable: tuple[str, ...] | None = None) -> None:
    context.profile.policy = demo_policy()
    context.profile.policy.preauthorized_records = [
        Preauthorized(tenant="aurora", entity="employee", id=str(i)) for i in range(1, 101)
    ]
    fields = list(FIELDS) if editable is None else list(editable)
    context.profile.editable_fields = {"employee": fields}


def call(runtime: BrowserTools, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(runtime.call(name, arguments))


async def edit_phone(runtime: BrowserTools) -> bool:
    """Edita o telefone do funcionário 1, recarrega e diz se o valor persistiu."""
    base = runtime.project.profile.target_url
    href = base + "/employees/1/edit"
    await runtime.call("navigate", {"url": href})
    await runtime.call("fill", {"target": PHONE, "value": NEW_PHONE})
    await runtime.call("submit", {"target": SAVE})
    await runtime.call("navigate", {"url": href})
    page = cast(Page, runtime.session.page)
    value = await page.get_by_label("Telefone").input_value()
    return value == NEW_PHONE


def explore(history: HistoryStore, context: ProjectContext, *, intermittent: bool = False) -> str:
    """Run explorada pelo mesmo caminho de tools: edição permitida e finding com evidência."""
    run_id = history.create_run(context, TextMission("Explore a edição do telefone")).record.id
    runtime = BrowserTools(context, history, run_id)

    async def scenario() -> None:
        try:
            await edit_phone(runtime)
            shot = await runtime.call("screenshot", {})
            await runtime.call(
                "report_finding",
                {
                    "title": "Telefone editado não persiste",
                    "flow": "Edição de funcionário",
                    "preconditions": [PRECONDITION],
                    "steps": [
                        "Abrir a edição do funcionário 1",
                        f"Preencher o telefone {NEW_PHONE}",
                        "Salvar",
                        "Recarregar a página de edição",
                    ],
                    "expected": {
                        "text": "O telefone salvo permanece após recarregar",
                        "source": "heuristic",
                    },
                    "observed": OBSERVED,
                    "evidence": [{"artifact_id": shot["artifact_id"], "purpose": "after"}],
                    "classification": "suspected",
                    "failure_domain": "system",
                    "intermittent": intermittent,
                },
            )
            await runtime.call("end_run", {"outcome": "completed"})
        finally:
            await runtime.session.close()

    asyncio.run(scenario())
    return run_id


@dataclass
class Attempt:
    run_id: str
    executor: str
    mission: str = ""
    marker: str = ""
    knowledge: object = None


async def replay_phone(runtime: BrowserTools, attempt: Attempt) -> None:
    """Reprodutor do defeito d02: replay dos passos e relato com evidência própria."""
    persisted = await edit_phone(runtime)
    shot = await runtime.call("screenshot", {})
    if persisted:
        await runtime.call(REPRODUCTION_TOOL, {"result": "not_reproduced"})
    else:
        await runtime.call(
            REPRODUCTION_TOOL,
            {"result": "reproduced", "evidence_artifact_ids": [shot["artifact_id"]]},
        )


async def missing_precondition(runtime: BrowserTools, attempt: Attempt) -> None:
    await runtime.call(
        REPRODUCTION_TOOL, {"result": "precondition_missing", "precondition_index": 0}
    )


async def busy(runtime: BrowserTools, attempt: Attempt) -> None:
    """Reprodutor que nunca termina: o teto de ações da tentativa encerra a run filha."""
    base = runtime.project.profile.target_url
    for _ in range(20):
        result = await runtime.call("navigate", {"url": base + "/employees"})
        if result.get("reason") == "write_lock":
            return


@dataclass
class DirectLauncher:
    """Reprodutor sem browser: relata pela fachada, como a tool faria, e encerra a run filha."""

    executor: str = "claude-code"
    result: ReproducerResult = ReproducerResult.REPRODUCED
    calls: list[str] = field(default_factory=list[str])

    def prepare(self, project: ProjectContext) -> PreparedChild:
        return PreparedChild(executor=ExecutorInfo(name=self.executor), skill_bundle_version=None)

    def launch(
        self,
        project: ProjectContext,
        history: HistoryStore,
        created: CreatedRun,
        prepared: PreparedChild,
    ) -> LaunchOutcome:
        run_id = created.record.id
        self.calls.append(run_id)
        evidence: tuple[str, ...] = ()
        if self.result == ReproducerResult.REPRODUCED:
            artifact = history.register_artifact(
                run_id, "evidence/replay.png", ArtifactType.SCREENSHOT, content=run_id.encode()
            )
            evidence = (artifact.id,)
        history.record_reproducer_report(run_id, self.result, evidence, None, "")
        history.finish_run(run_id, RunStatus.COMPLETED)
        return LaunchOutcome(status=RunStatus.COMPLETED, reason=None)


def suspected(history: HistoryStore, context: ProjectContext, titles: tuple[str, ...]) -> str:
    """Run explorada terminada com findings suspected, sem browser."""
    run_id = history.create_run(context, TextMission("Explore")).record.id
    for index, title in enumerate(titles):
        artifact = history.register_artifact(
            run_id, f"evidence/{index}.png", ArtifactType.SCREENSHOT, content=title.encode()
        )
        history.report_finding(
            run_id,
            FindingDraft(
                title=title,
                flow="Cadastro",
                preconditions=(PRECONDITION,),
                steps=("Abrir cadastro", "Salvar"),
                expected="Recusa",
                expected_source=ExpectedSource.HEURISTIC,
                expected_source_ref=None,
                observed="Aceito",
                evidence=(EvidenceReference(artifact.id, EvidencePurpose.STATE),),
                classification=FindingClassification.SUSPECTED,
                limitations=(),
                failure_domain=FailureDomain.SYSTEM,
            ),
        )
    history.finish_run(run_id, RunStatus.COMPLETED)
    return run_id


@dataclass
class ScriptedLauncher:
    """Adaptador de teste da porta: mesma run filha, mesmas tools, roteiro no lugar do modelo."""

    executor: str
    script: Script
    available: bool = True
    attempts: list[Attempt] = field(default_factory=list[Attempt])
    fail_before: int | None = None

    def prepare(self, project: ProjectContext) -> PreparedChild:
        if not self.available:
            raise ExecutorUnavailable(f"{self.executor} unavailable: binary not found.")
        if self.fail_before is not None and len(self.attempts) + 1 == self.fail_before:
            self.fail_before = None
            raise KeyboardInterrupt
        return PreparedChild(executor=ExecutorInfo(name=self.executor), skill_bundle_version=None)

    def launch(
        self,
        project: ProjectContext,
        history: HistoryStore,
        created: CreatedRun,
        prepared: PreparedChild,
    ) -> LaunchOutcome:
        attempt = Attempt(run_id=created.record.id, executor=self.executor)
        self.attempts.append(attempt)
        runtime = BrowserTools(project, history, created.record.id)

        async def scenario() -> None:
            try:
                context = await runtime.call("mission_context", {})
                attempt.mission = str(context.get("content", ""))
                attempt.knowledge = context.get("knowledge")
                data = await runtime.call("generate_test_data", {"entity": "employee"})
                attempt.marker = str(data.get("marker", ""))
                await self.script(runtime, attempt)
                # Parada por limite é decidida pelo servidor; o roteiro só espera o encerramento.
                if runtime.monitor.stopping is not None:
                    await runtime.monitor.stopping
                if not history.get_run(created.record.id).status.is_terminal:
                    await runtime.call("end_run", {"outcome": "completed"})
            finally:
                await runtime.session.close()

        asyncio.run(scenario())
        record = history.get_run(created.record.id)
        return LaunchOutcome(status=record.status, reason=record.status_reason)
