"""Dublês do modo autônomo: executor, rede e browser substituíveis, sem custo nem SO."""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from lastro.autonomous.models import MissionSnapshot, TriggerInput, TriggerKind
from lastro.autonomous.runner import AutonomousCoordinator, AutonomousPorts
from lastro.config.loader import ProjectContext
from lastro.evaluation.executors import PreparedRun, RunLaunch
from lastro.evaluation.models import (
    ExecutorIdentity,
    HeadlessResult,
    HeadlessTask,
    StopReason,
)
from lastro.history import HistoryStore, RunStatus

MISSION = MissionSnapshot("file", "missions/daily.md", "Explore the employee list")

type Script = Callable[[RunLaunch], None]


def complete(launch: RunLaunch) -> None:
    launch.history.finish_run(launch.run_id, RunStatus.COMPLETED, None)


@dataclass
class FakeExecutor:
    """Executor simulado: aplica o roteiro na run e registra cada lançamento."""

    script: Script = complete
    reachable: bool = True
    auth: str | None = None
    launches: list[RunLaunch] = field(default_factory=list[RunLaunch])
    tasks: list[HeadlessTask] = field(default_factory=list[HeadlessTask])
    collected: list[str] = field(default_factory=list[str])
    context: Any = None

    def ports(self) -> AutonomousPorts:
        return AutonomousPorts(
            reachable=lambda url: self.reachable,
            authenticate=lambda project: self.auth,
            prepare=self.prepare,
            launch=self.launch,
            collect=self.collect,
            reproduce=lambda project, history, run_id, write: None,
        )

    def prepare(self, task: HeadlessTask, project: ProjectContext) -> PreparedRun:
        self.tasks.append(task)
        return PreparedRun(task.executor, "1.0-test", "c" * 64)

    def launch(self, launch: RunLaunch, cancel: threading.Event) -> HeadlessResult:
        self.launches.append(launch)
        self.script(launch)
        return HeadlessResult(
            status=StopReason.COMPLETED,
            executor=ExecutorIdentity(launch.task.executor, "1.0-test"),
            run_ref=launch.run_id,
        )

    def collect(self, project: ProjectContext, history: HistoryStore, item: Any) -> Any:
        self.collected.append(item.id)
        return self.context


def coordinator(
    context: ProjectContext, history: HistoryStore, fake: FakeExecutor
) -> AutonomousCoordinator:
    return AutonomousCoordinator(context.loaded, history, fake.ports())


def manual(key: str = "manual:1", profile: str = "homolog", **overrides: Any) -> TriggerInput:
    data: dict[str, Any] = {
        "kind": TriggerKind.MANUAL,
        "key": key,
        "profile": profile,
        "mission": MISSION,
    }
    data.update(overrides)
    return TriggerInput(**data)


PLAN = {
    "objectives": ["Validar o cadastro"],
    "scope": ["employee.create"],
    "criteria": [{"text": "O cadastro valida o CPF", "source": "mission_explicit"}],
    "gaps": [],
    "out_of_scope": [],
    "charter": {
        "target": "employee registration",
        "resources": ["demo application"],
        "information": "validation defects",
        "heuristics": ["boundary_values"],
    },
}
STEPS = ("Abrir /employees/new", "Preencher CPF 000.000.000-00", "Clicar em #save")


def found(
    title: str = "CPF inválido é aceito",
    flow: str = "employee.create",
    steps: tuple[str, ...] = STEPS,
    observed: str = "O cadastro foi salvo com CPF inválido",
) -> dict[str, Any]:
    return {"title": title, "flow": flow, "steps": steps, "observed": observed}


def explore(findings: list[dict[str, Any]], *, evaluate: bool = True) -> Script:
    """Roteiro de run: plano, critério avaliado e achados com evidência registrada."""

    def script(launch: RunLaunch) -> None:
        from lastro.history import (
            ArtifactType,
            CriterionOutcome,
            CriterionResultDraft,
            EvidencePurpose,
            EvidenceReference,
            ExpectedSource,
            FailureDomain,
            FindingClassification,
            FindingDraft,
        )
        from lastro.mission import MissionPlanInput

        history = launch.history
        run_id = launch.run_id
        plan = history.submit_mission_plan(run_id, MissionPlanInput.model_validate(PLAN)).plan
        if evaluate:
            history.record_criterion_result(
                run_id,
                CriterionResultDraft(
                    plan.criteria[0].id, CriterionOutcome.NOT_MET, (), "falhou", "observed"
                ),
            )
        for index, data in enumerate(findings):
            shot = history.register_artifact(
                run_id,
                f"evidence/shot-{index}.png",
                ArtifactType.SCREENSHOT,
                content=f"png-{index}".encode(),
            )
            history.report_finding(
                run_id,
                FindingDraft(
                    title=data["title"],
                    flow=data["flow"],
                    preconditions=(),
                    steps=tuple(data["steps"]),
                    expected="O CPF inválido é recusado",
                    expected_source=ExpectedSource.HEURISTIC,
                    expected_source_ref=None,
                    observed=data["observed"],
                    evidence=(EvidenceReference(shot.id, EvidencePurpose.STATE),),
                    classification=FindingClassification.SUSPECTED,
                    limitations=(),
                    failure_domain=FailureDomain.SYSTEM,
                ),
            )
        history.finish_run(run_id, RunStatus.COMPLETED, None)

    return script


@dataclass
class RecordingAdapter:
    """Canal simulado: devolve resultados roteirizados e guarda as mensagens recebidas."""

    results: list[Any] = field(default_factory=list[Any])
    sent: list[Any] = field(default_factory=list[Any])

    def send(self, message: Any, channel: Any, environ: Any) -> Any:
        from lastro.autonomous.models import AttemptStatus, DeliveryResult

        self.sent.append(message)
        if self.results:
            return self.results.pop(0)
        return DeliveryResult(AttemptStatus.DELIVERED, 200)


CHANNELS = """
[autonomous]
enabled = true

[[autonomous.notifications]]
name = "qa-slack"
type = "slack"
webhook_env = "LASTRO_TEST_SLACK"

[[autonomous.notifications]]
name = "qa-teams"
type = "teams"
webhook_env = "LASTRO_TEST_TEAMS"

[[autonomous.notifications]]
name = "qa-email"
type = "smtp"
host = "smtp.example.test"
from_address = "lastro@example.test"
to = ["qa@example.test"]

"""


def with_channels(context: ProjectContext) -> ProjectContext:
    """Reescreve o lastro.toml com os três canais e recarrega o contexto."""
    from lastro.config.loader import load_project

    path = context.config_path
    text = path.read_text(encoding="utf-8")
    first = text.index("[profiles.")
    path.write_text(text[:first] + CHANNELS + text[first:], encoding="utf-8")
    return load_project(path, context.profile_name, cwd=path.parent)
