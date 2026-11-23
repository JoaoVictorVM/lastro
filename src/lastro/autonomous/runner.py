"""Coordenador autônomo: drena a fila de um perfil com lease, uma run normal por item.

Para cada item: preflight (configuração, missão congelada, executor, alvo e autenticação),
contexto de mudança nos triggers pós-deploy, criação da run com snapshot efetivo, execução
headless unattended com prazo externo e, depois do término, relatório e entrega. O id do item é
a chave idempotente de cada fase; uma retomada reconcilia a run existente e nunca cria outra.
"""

import asyncio
import contextlib
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx

from lastro.autonomous.comparison import compare, failed_result, to_view
from lastro.autonomous.models import (
    LEASE_BUSY,
    LEASE_LOST,
    PREFLIGHT_FAILED,
    AutonomousError,
    ComparisonResult,
    QueueItem,
    QueueStatus,
)
from lastro.autonomous.notifications import Outbox, build_message
from lastro.autonomous.queue import AutonomousQueue, LeaseKeeper
from lastro.config.loader import LoadedConfig, ProjectContext, select_profile
from lastro.evaluation.decisions import DecisionLoop, UnattendedDecisions
from lastro.evaluation.executors import (
    GRACE_SECONDS,
    PreparedRun,
    RunLaunch,
    launch_run,
    prepare_headless,
)
from lastro.evaluation.models import (
    EvalError,
    GateMode,
    HeadlessResult,
    HeadlessTask,
    TaskPurpose,
)
from lastro.history import (
    ActiveRunExistsError,
    EventType,
    ExecutorInfo,
    HistoryError,
    HistoryStore,
    RunRecord,
    RunStatus,
    TextMission,
)

REACHABILITY_TIMEOUT = httpx.Timeout(10.0)
UNREACHABLE = "unreachable"
AUTH = "auth"
RECOVERED_REASON = "autonomous_recovered"
UNAVAILABLE_STATUSES = frozenset({502, 503, 504})
POST_DEPLOY_DIRECTIVE = (
    "\n\n## Autonomous post-deployment run\n\n"
    "This run was triggered by a successful deployment ({origin}). Keep every mission "
    "criterion, but list the flows touched by the change context first in `affected_flows`. "
    "The association between the change and each flow is a hypothesis derived from the change "
    "context, not a requirement."
)

type Writer = Callable[[str], None]


def _no_write(line: str) -> None:
    return None


def default_reachable(url: str) -> bool:
    """Alvo indisponível: falha de conexão, timeout ou 502/503/504; TLS nunca é desligado."""
    try:
        response = httpx.get(url, timeout=REACHABILITY_TIMEOUT, follow_redirects=False)
    except httpx.HTTPError:
        return False
    return response.status_code not in UNAVAILABLE_STATUSES


def default_authenticate(project: ProjectContext) -> str | None:
    """None quando login e tenant conferem; `auth` quando a credencial falha ou expirou."""
    from playwright.async_api import Error as PlaywrightError

    from lastro.auth import AuthError, run_auth_check
    from lastro.auth.credentials import resolve_credentials

    try:
        resolve_credentials(project.profile)
        outcome = asyncio.run(run_auth_check(project))
    except AuthError:
        return AUTH
    except PlaywrightError:
        raise AutonomousError(PREFLIGHT_FAILED, "Chromium is not available for login") from None
    return None if outcome.result.is_verified else AUTH


def default_collect(project: ProjectContext, history: HistoryStore, item: QueueItem) -> Any:
    """Coleta controlada da F19 antes do handoff; sem `[source]` válido, segue sem contexto."""
    from lastro.change_context.collector import Collector, CollectRequest
    from lastro.change_context.models import ComparisonMode, PreparedContext
    from lastro.change_context.scanner import Gitleaks
    from lastro.config.source import resolve_source_root

    settings = project.config.source
    root = resolve_source_root(settings)
    scanner = Gitleaks()
    scanner.ensure()

    def baseline(flow: str) -> tuple[str, str] | None:
        return history.last_passed_baseline(project.profile_name, project.profile.tenant_id, flow)

    draft = Collector(settings, root, scanner=scanner, baseline=baseline).collect(
        CollectRequest(mode=ComparisonMode.FEATURE)
    )
    return PreparedContext(change=draft)


def default_reproduce(
    project: ProjectContext, history: HistoryStore, run_id: str, write: Writer
) -> None:
    """Reprodução automática como numa run normal, quando o perfil a habilita."""
    from lastro.reproduction.trigger import auto_reproduce

    auto_reproduce(project, history, run_id, write=write)


@dataclass
class AutonomousPorts:
    """Pontos de injeção: rede, browser, executor e coleta substituíveis nos testes."""

    reachable: Callable[[str], bool] = default_reachable
    authenticate: Callable[[ProjectContext], str | None] = default_authenticate
    prepare: Callable[[HeadlessTask, ProjectContext], PreparedRun] = prepare_headless
    launch: Callable[[RunLaunch, threading.Event], HeadlessResult] = launch_run
    collect: Callable[[ProjectContext, HistoryStore, QueueItem], Any] = default_collect
    reproduce: Callable[[ProjectContext, HistoryStore, str, Writer], None] = default_reproduce
    # Argumentos extras da outbox (adapters, ambiente, relógio) para testes.
    outbox: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True)
class ItemOutcome:
    item: QueueItem
    run: RunRecord | None
    report_path: Path | None = None
    limitations: tuple[str, ...] = ()

    def as_json(self) -> dict[str, Any]:
        return {
            "item_id": self.item.id,
            "status": self.item.status.value,
            "reason": self.item.reason,
            "run_id": self.run.id if self.run else None,
            "run_status": self.run.status.value if self.run else None,
            "report": str(self.report_path) if self.report_path else None,
        }


def effective_project(loaded: LoadedConfig, item: QueueItem) -> ProjectContext:
    """Perfil com o menor limite de duração e de custo nativo; o lastro.toml não muda."""
    config = loaded.config
    settings = config.autonomous
    profile = select_profile(config, item.profile)
    limits = profile.run_limits.model_copy(
        update={"max_minutes": min(settings.max_minutes, profile.run_limits.max_minutes)}
    )
    cap = min(Decimal(settings.native_max_cost_usd), Decimal(profile.native.cost_cap_usd))
    native = profile.native.model_copy(update={"cost_cap_usd": format(cap, "f")})
    profile = profile.model_copy(update={"run_limits": limits, "native": native})
    profile.name = item.profile
    updated = config.model_copy(
        update={
            "executor": item.executor or config.executor,
            "profiles": {**config.profiles, item.profile: profile},
        }
    )
    return ProjectContext(LoadedConfig(loaded.path, updated), profile)


def run_snapshot(project: ProjectContext, item: QueueItem) -> dict[str, Any]:
    """Snapshot efetivo da run: vínculo com o trigger, missão congelada e limites, sem segredos."""
    settings = project.config.autonomous.snapshot()
    return {
        "autonomous": {
            "item_id": item.id,
            "trigger": item.trigger_kind.value,
            "trigger_ref": item.trigger_key,
            "schedule_id": item.schedule_id,
            "mission_ref": item.mission.ref,
            "mission_sha256": item.mission.sha256,
            "gate_mode": GateMode.UNATTENDED.value,
            "max_minutes": project.profile.run_limits.max_minutes,
            "native_cost_cap_usd": project.profile.native.cost_cap_usd,
            "channels": settings["channels"],
        }
    }


def _item_status(record: RunRecord) -> QueueStatus:
    if record.status == RunStatus.BLOCKED:
        return QueueStatus.BLOCKED
    if record.status in {RunStatus.COMPLETED, RunStatus.LIMIT_REACHED}:
        return QueueStatus.COMPLETED
    return QueueStatus.FAILED


@dataclass
class AutonomousCoordinator:
    loaded: LoadedConfig
    history: HistoryStore
    ports: AutonomousPorts = field(default_factory=AutonomousPorts)
    write: Writer = _no_write
    queue: AutonomousQueue | None = None

    def __post_init__(self) -> None:
        if self.queue is None:
            self.queue = AutonomousQueue(self.history.autonomous)

    @property
    def repository(self) -> Any:
        return self.history.autonomous

    def drain(self, profile: str) -> list[ItemOutcome]:
        """Processa itens enquanto o lease for deste worker; ocupado devolve lista vazia."""
        assert self.queue is not None
        keeper = self.queue.acquire(profile)
        if keeper is None:
            return []
        outcomes: list[ItemOutcome] = []
        try:
            while True:
                item = self.queue.claim(keeper)
                if item is None:
                    break
                outcomes.append(self.process(keeper, item))
        except AutonomousError as exc:
            if exc.code != LEASE_LOST:
                raise
            self.write(f"Lease lost for profile {profile}; stopping without new effects.")
        finally:
            keeper.stop()
        return outcomes

    def process(self, keeper: LeaseKeeper, item: QueueItem) -> ItemOutcome:
        if item.run_id is not None:
            return self._finalize(keeper, item, self._reconcile(item.run_id))
        item = self.repository.mark_running(keeper.ensure(), item.id)
        try:
            project = effective_project(self.loaded, item)
            task = HeadlessTask(
                task_id=item.id,
                purpose=TaskPurpose.AUTONOMOUS,
                executor=project.config.executor,
                model=None,
                workspace=project.loaded.project_dir,
                mission_ref=item.mission.ref or f"queue:{item.id}",
                gate_mode=GateMode.UNATTENDED,
            )
            prepared = self.ports.prepare(task, project)
        except (EvalError, HistoryError) as exc:
            return self._fail(keeper, item, PREFLIGHT_FAILED, str(exc))
        except Exception as exc:
            return self._fail(keeper, item, PREFLIGHT_FAILED, type(exc).__name__)
        try:
            blocked = self._preflight_block(project)
        except AutonomousError as exc:
            return self._fail(keeper, item, exc.code, exc.message)
        limitations: list[str] = []
        prepared_context: Any = None
        text = item.mission.text
        if item.trigger_kind.post_deploy:
            payload = cast(dict[str, Any], item.trigger_snapshot.get("payload") or {})
            origin = ", ".join(
                str(value)
                for value in (
                    payload.get("workflow") or payload.get("environment"),
                    payload.get("head_sha"),
                )
                if value
            )
            text += POST_DEPLOY_DIRECTIVE.format(origin=origin or item.trigger_kind.value)
            if blocked is None:
                try:
                    prepared_context = self.ports.collect(project, self.history, item)
                except Exception as exc:
                    limitations.append(f"Change context unavailable: {type(exc).__name__}.")
        try:
            keeper.ensure()
            created = self.history.create_run(
                project,
                TextMission(text),
                ExecutorInfo(name=prepared.executor, version=prepared.version),
                skill_bundle_version=prepared.skill_bundle_version,
                prepared=prepared_context,
                executor_snapshot={
                    **(prepared.executor_snapshot or {}),
                    **run_snapshot(project, item),
                },
            )
        except ActiveRunExistsError as exc:
            return self._fail(keeper, item, "active_run_exists", f"run {exc.run_id} is active")
        except HistoryError as exc:
            return self._fail(keeper, item, PREFLIGHT_FAILED, str(exc))
        run_id = created.record.id
        item = self.repository.link_run(keeper.ensure(), item.id, run_id)
        context = self.history.get_change_context(run_id)
        if context is not None:
            self.repository.set_change_context(keeper.ensure(), item.id, context.id)
        self.repository.append_run_event(
            run_id,
            EventType.AUTONOMOUS_TRIGGERED,
            {
                "item_id": item.id,
                "trigger": item.trigger_kind.value,
                "schedule_id": item.schedule_id,
                "mission_sha256": item.mission.sha256,
                "limitations": limitations,
            },
            f"{item.id}:triggered",
        )
        if blocked is not None:
            self.history.finish_run(run_id, RunStatus.BLOCKED, blocked)
            if blocked == AUTH:
                self.write("Authentication failed or expired; run 'lastro auth check'.")
        else:
            self._execute(keeper, task, prepared, project, run_id)
        return self._finalize(keeper, item, self.history.get_run(run_id), tuple(limitations))

    def _preflight_block(self, project: ProjectContext) -> str | None:
        """Alvo indisponível ou autenticação falha viram run blocked, sem retry da missão."""
        if not self.ports.reachable(project.profile.target_url):
            return UNREACHABLE
        return self.ports.authenticate(project)

    def _execute(
        self,
        keeper: LeaseKeeper,
        task: HeadlessTask,
        prepared: PreparedRun,
        project: ProjectContext,
        run_id: str,
    ) -> None:
        minutes = project.profile.run_limits.max_minutes
        cancel = threading.Event()
        done = threading.Event()
        decisions = DecisionLoop(UnattendedDecisions(self.history).tick)

        def watchdog() -> None:
            # Prazo externo: duração máxima mais folga; lease perdido também interrompe.
            deadline = minutes * 60 + GRACE_SECONDS
            waited = 0.0
            while not done.wait(1.0):
                waited += 1.0
                if keeper.lost.is_set() or waited >= deadline:
                    cancel.set()
                    return

        guard = threading.Thread(target=watchdog, name="autonomous-watchdog", daemon=True)
        log_dir = self.history.run_dir(run_id) / "autonomous"
        launch = RunLaunch(
            task, prepared, project, self.history, run_id, minutes, log_dir, self.write
        )
        decisions.start()
        guard.start()
        try:
            try:
                self.ports.launch(launch, cancel)
            except EvalError as exc:
                self.write(f"Executor stopped: {exc.code}")
            with contextlib.suppress(HistoryError):
                if not self.history.get_run(run_id).status.is_terminal:
                    self.history.finish_run(run_id, RunStatus.INTERRUPTED, "autonomous_supervisor")
            # Filhas de reprodução também seguem a política unattended do loop ainda ativo.
            if not cancel.is_set():
                with contextlib.suppress(Exception):
                    self.ports.reproduce(project, self.history, run_id, self.write)
        finally:
            done.set()
            decisions.close()
            guard.join(timeout=5)

    def _reconcile(self, run_id: str) -> RunRecord:
        """Item retomado após crash: a run existente termina como interrompida, nunca é recriada."""
        record = self.history.get_run(run_id)
        if not record.status.is_terminal:
            with contextlib.suppress(HistoryError):
                record = self.history.finish_run(run_id, RunStatus.INTERRUPTED, RECOVERED_REASON)
        return record

    def _fail(self, keeper: LeaseKeeper, item: QueueItem, code: str, detail: str) -> ItemOutcome:
        self.write(f"Autonomous item {item.id} failed before the run: {detail} ({code})")
        finished = self.repository.finish_item(keeper.ensure(), item.id, QueueStatus.FAILED, code)
        self._notify(keeper, finished, None, None, None)
        return ItemOutcome(finished, None)

    def _finalize(
        self,
        keeper: LeaseKeeper,
        item: QueueItem,
        record: RunRecord,
        limitations: tuple[str, ...] = (),
    ) -> ItemOutcome:
        from lastro.reporting.builder import ReportBuilder

        builder = ReportBuilder(self.history)
        with contextlib.suppress(Exception):
            builder.build_final(record.id)
        comparison = self._compare(keeper, item, record)
        pending = self.history.pending_items(record.id)
        view = to_view(
            self.history,
            comparison,
            item.trigger_kind.value,
            skipped_approvals=sum(
                1 for p in pending if p.kind == "approval" and p.state == "skipped_unattended"
            ),
            clarification_gaps=sum(1 for p in pending if p.kind == "clarification_gap"),
        )
        report_path: Path | None = None
        snapshot_id: str | None = None
        try:
            keeper.ensure()
            report = builder.build_autonomous(record.id, view)
            # Falha de template cai no fallback JSON do builder; a notificação aponta para ele.
            report_path = report.report_path or report.fallback_path
            snapshot_id = report.snapshot.id
        except AutonomousError:
            raise
        except Exception as exc:
            self.write(f"Autonomous report unavailable: {type(exc).__name__}")
        finished = self.repository.finish_item(
            keeper.ensure(), item.id, _item_status(record), record.status_reason, snapshot_id
        )
        run = self.history.get_run(record.id)
        self._notify(keeper, finished, run, comparison, report_path)
        return ItemOutcome(finished, run, report_path, limitations)

    def _compare(self, keeper: LeaseKeeper, item: QueueItem, record: RunRecord) -> ComparisonResult:
        """Uma comparação por item; retomada lê a persistida em vez de recalcular."""
        stored = self.repository.comparison_for_item(item.id)
        if stored is None:
            try:
                result = compare(self.history, item, record)
            except Exception as exc:
                result = failed_result(record, type(exc).__name__)
            keeper.ensure()
            stored = self.repository.save_comparison(item.id, result)
        self.repository.append_run_event(
            record.id,
            EventType.AUTONOMOUS_COMPARED,
            {
                "comparison_id": stored.id,
                "status": stored.result.status.value,
                "baseline_run_id": stored.result.baseline_run_id,
                "counts": stored.result.counts,
                "algorithm": stored.result.algorithm,
            },
            f"{item.id}:compared",
        )
        return stored.result

    def _notify(
        self,
        keeper: LeaseKeeper,
        item: QueueItem,
        run: RunRecord | None,
        comparison: ComparisonResult | None,
        report_path: Path | None,
    ) -> None:
        """Programa a outbox e faz a primeira tentativa; falha de canal não muda a run."""
        message = build_message(self.history, item, run, comparison, report_path)
        keeper.ensure()
        outbox = self.outbox()
        if outbox.schedule(item, message):
            outbox.deliver_due(item.id)

    def outbox(self) -> Outbox:
        return Outbox(self.history, self.loaded.config.autonomous, **self.ports.outbox)


def drain_profile(
    loaded: LoadedConfig,
    history: HistoryStore,
    profile: str,
    *,
    ports: AutonomousPorts | None = None,
    write: Writer = _no_write,
) -> list[ItemOutcome]:
    coordinator = AutonomousCoordinator(loaded, history, ports or AutonomousPorts(), write)
    return coordinator.drain(profile)


def lease_busy(profile: str) -> AutonomousError:
    return AutonomousError(LEASE_BUSY, f"another worker owns profile {profile}")
