"""Coordenador sequencial e durável da suite: lease, tentativas, coleta, pausa e retomada.

Cada trial exploratório começa com reset e versão compatível; reproduções acontecem no mesmo
demo, sem reset entre a exploração e suas filhas. Crash invalida só o trial; limite do provedor
pausa a suite e o slot recomeça com nova tentativa no resume.
"""

import contextlib
import hashlib
import json
import os
import secrets
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lastro.evaluation import conditions
from lastro.evaluation.decisions import DecisionLoop, TrialDecisions
from lastro.evaluation.demo import DemoPort, check_version
from lastro.evaluation.executors import HeadlessExecutor, JudgePort, TrialLaunch, task_for
from lastro.evaluation.models import (
    AttemptStatus,
    Condition,
    EvalError,
    GateMode,
    HeadlessResult,
    InvalidReason,
    StopReason,
    SuiteSpec,
    SuiteStatus,
    TrialRole,
    TrialStatus,
)
from lastro.evaluation.suites import (
    LoadedSuite,
    catalog_bytes,
    expand_slots,
    manifest,
    rubric_text,
    sha256_text,
    spec_from_manifest,
)
from lastro.evaluation.workspace import (
    WorkspaceRequest,
    copy_memory,
    history_relative_template,
    prepare_workspace,
    suite_root,
    trial_paths,
)
from lastro.history import HistoryStore
from lastro.history.evaluation import (
    HEARTBEAT_SECONDS,
    AttemptRecord,
    EvalRepository,
    EvalStoreError,
    Lease,
    NewSuite,
    NewTrial,
    SuiteRecord,
    TrialRecord,
)
from lastro.history.layout import write_bytes_atomically

DEFAULT_TENANT = ("aurora", "Clínica Aurora")
NUSD_PER_USD = 1_000_000_000
# Reserva por exploração quando o custo é controlável pelo Lastro (ledger do executor nativo);
# executores com assinatura não reportam custo antes da chamada e reservam zero.
NATIVE_RESERVATION_NUSD = 2 * NUSD_PER_USD


def usd_nusd(value: str) -> int:
    from decimal import Decimal

    return int(Decimal(value) * NUSD_PER_USD)


type ProgressSink = Callable[["Progress"], None]


@dataclass(frozen=True)
class Progress:
    done: int
    total: int
    condition: str
    repetition: int
    runs_per_condition: int
    eta_seconds: float | None
    status: str


@dataclass(frozen=True)
class SuiteOutcome:
    suite: SuiteRecord
    exit_code: int
    message: str = ""


def process_alive(owner: dict[str, Any]) -> bool:
    """Dono anterior vivo no mesmo host; PID reutilizado não é confundido pelo token de início."""
    from lastro_demo import _process

    if owner.get("host") != socket.gethostname():
        return True
    pid = owner.get("pid")
    if not isinstance(pid, int):
        return False
    return _process.pid_alive(pid)


def owner_identity() -> dict[str, Any]:
    return {
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "nonce": secrets.token_hex(8),
        "started": time.time(),
    }


@dataclass
class Heartbeat:
    repo: EvalRepository
    lease: Lease
    interval: float = HEARTBEAT_SECONDS
    lost: threading.Event = field(default_factory=threading.Event)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                if not self.repo.heartbeat(self.lease):
                    self.lost.set()
                    return
            except Exception:
                self.lost.set()
                return

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="eval-lease", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


@dataclass
class RunnerDeps:
    history: HistoryStore
    project_dir: Path
    project_id: str
    demo: DemoPort
    executor: HeadlessExecutor
    judge: JudgePort | None
    credentials: tuple[str, str]
    progress: ProgressSink = lambda progress: None
    write: Callable[[str], None] = lambda line: None
    baseline_login: Callable[[str, Path], None] | None = None
    owner_gone: Callable[[dict[str, Any]], bool] = lambda owner: not process_alive(owner)
    monotonic: Callable[[], float] = time.monotonic
    decision_sleep: Callable[[float], None] = time.sleep
    heartbeat_interval: float = HEARTBEAT_SECONDS


def system_identity(version: dict[str, Any], variant: str) -> str:
    """Versão do pacote do demo mais variante: mudar a variante torna a memória anterior stale."""
    return f"lastro-demo {version.get('version', 'unknown')}+{variant}"


def condition_sha256(condition: Condition) -> str:
    return conditions.EffectiveCondition.of(condition).sha256


def create_suite(
    repo: EvalRepository, loaded: LoadedSuite, executor_name: str, executor_version: str
) -> SuiteRecord:
    """Congela suite, hashes, manifest e ordem antes de qualquer sessão de agente."""
    spec = loaded.spec
    slots = expand_slots(spec)
    frozen = manifest(loaded, slots, executor_name)
    trials = [
        NewTrial(
            ordinal=slot.ordinal,
            condition=slot.condition.value,
            scenario=slot.scenario,
            variant=slot.variant,
            repetition=slot.repetition,
            role=slot.role.value,
            status=(
                TrialStatus.PENDING.value if slot.applicable else TrialStatus.NOT_APPLICABLE.value
            ),
            condition_sha256=condition_sha256(slot.condition),
            memory_sha256=None,
            parent_ordinal=slot.parent_ordinal,
        )
        for slot in slots
    ]
    return repo.create_suite(
        NewSuite(
            suite_name=spec.id,
            suite_sha256=loaded.raw_sha256,
            manifest=frozen,
            catalog_sha256=hashlib.sha256(catalog_bytes()).hexdigest(),
            rubric_sha256=sha256_text(rubric_text()),
            executor_name=executor_name,
            executor_version=executor_version,
            model_identity={"requested": spec.model, "judge": spec.judge.model},
            seed=spec.seed,
            trials=trials,
        )
    )


def _evidence_ok(history: HistoryStore, run_id: str) -> tuple[bool, set[str]]:
    from lastro.reporting.integrity import verify_artifacts

    artifacts = history.list_artifacts(run_id)
    report = verify_artifacts(history.run_dir(run_id), artifacts)
    broken = {problem.relative_path for problem in report.problems}
    valid = {item.id for item in artifacts if item.relative_path not in broken}
    return report.valid, valid


def opaque(*parts: str) -> str:
    return "f_" + hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()[:16]


def collect_lastro(
    history: HistoryStore, run_id: str, criteria_ids: tuple[str, ...]
) -> dict[str, Any]:
    """Findings, critérios, vereditos e ações lidos do histórico estruturado, não do Markdown."""
    from lastro.history import EventType
    from lastro.history.models import ClarificationStatus

    _, valid_artifacts = _evidence_ok(history, run_id)
    verdicts = {item.finding_id: item for item in history.list_reproduction_verdicts(run_id)}
    findings: list[dict[str, Any]] = []
    for finding in history.list_findings(run_id):
        evidence_ids = [item.artifact_id for item in finding.evidence]
        verdict = verdicts.get(finding.id)
        findings.append(
            {
                "ref": opaque(run_id, finding.id),
                "title": finding.title,
                "flow": finding.flow,
                "steps": list(finding.steps),
                "expected": finding.expected,
                "observed": finding.observed,
                "evidence": [opaque(run_id, item) for item in evidence_ids],
                "evidence_ok": bool(evidence_ids)
                and all(item in valid_artifacts for item in evidence_ids),
                "classification": finding.classification.value,
                "confirmation_source": "reproduction" if verdict is not None else None,
                "verdict": None
                if verdict is None
                else {
                    "classification": verdict.classification.value,
                    "reproduced_count": verdict.reproduced_count,
                    "attempts_decisive": verdict.attempts_decisive,
                    "attempts_planned": verdict.attempts_planned,
                    "reason": verdict.reason.value if verdict.reason else None,
                },
            }
        )
    results = history.list_criterion_results(run_id)
    latest: dict[str, Any] = {}
    for result in results:
        latest[result.criterion_id] = result
    criteria = [
        {
            "id": key,
            "outcome": item.outcome.value,
            "evidence_ok": bool(item.evidence_artifact_ids)
            and all(ref in valid_artifacts for ref in item.evidence_artifact_ids),
        }
        for key, item in latest.items()
    ]
    events = history.list_events(run_id)
    by_tool: dict[str, int] = {}
    precondition_errors = 0
    for event in events:
        if event.event_type != EventType.TOOL_CALL:
            continue
        name = event.tool_name or ""
        if name in BROWSER_ACTION_TOOLS:
            by_tool[name] = by_tool.get(name, 0) + 1
        payload = event.payload if isinstance(event.payload, dict) else {}
        error = payload.get("error")
        if isinstance(error, str) and "precondition" in error:
            precondition_errors += 1
    questions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in history.list_clarifications(run_id):
        from lastro.evaluation.decisions import gap_key

        key = gap_key(item.gap_id)
        answered_before = key in seen
        questions.append({"key": key, "repeated": answered_before})
        if item.status == ClarificationStatus.ANSWERED:
            seen.add(key)
    approvals: list[dict[str, Any]] = []
    for record in history.list_approvals(run_id):
        action = record.payload.get("action")
        operation = action.get("operation") if isinstance(action, dict) else None
        approvals.append(
            {
                "decision": record.decision.value if record.decision else None,
                "decided_by": record.decided_by.value if record.decided_by else None,
                "consumed": record.consumed_at is not None,
                "operation": operation if isinstance(operation, str) else None,
            }
        )
    return {
        "findings": findings,
        "criteria": criteria,
        "criteria_ids": list(criteria_ids),
        "actions": {"total": sum(by_tool.values()), "by_tool": by_tool},
        "questions": questions,
        "precondition_errors": {"exploration": precondition_errors},
        "approvals": approvals,
        "status": history.get_run(run_id).status.value,
    }


BROWSER_ACTION_TOOLS = frozenset(
    {"observe", "navigate", "click", "fill", "select", "submit", "screenshot", "wait_for"}
)


def collect_baseline(trial_dir: Path, criteria_ids: tuple[str, ...]) -> dict[str, Any]:
    from lastro.evaluation.baseline import ACTIONS_FILE, ActionLog, collect_output

    output, error = collect_output(trial_dir)
    entries = ActionLog(trial_dir / ACTIONS_FILE).entries()
    by_tool: dict[str, int] = {}
    for item in entries:
        name = str(item.get("tool"))
        if name in BROWSER_ACTION_TOOLS:
            by_tool[name] = by_tool.get(name, 0) + 1
    findings: list[dict[str, Any]] = []
    criteria: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    if output is not None:
        for index, finding in enumerate(output.findings):
            findings.append(
                {
                    "ref": opaque(str(trial_dir.name), str(index)),
                    "title": finding.title,
                    "flow": finding.flow,
                    "steps": finding.steps,
                    "expected": finding.expected,
                    "observed": finding.observed,
                    "evidence": [opaque(str(trial_dir.name), ref) for ref in finding.evidence],
                    "evidence_ok": bool(finding.evidence),
                    "classification": finding.classification,
                    "confirmation_source": "agent",
                    "verdict": None,
                }
            )
        criteria = [
            {"id": item.id, "outcome": item.outcome, "evidence_ok": bool(item.evidence)}
            for item in output.criteria
        ]
        questions = [{"key": item.key, "repeated": False} for item in output.questions]
    return {
        "findings": findings,
        "criteria": criteria,
        "criteria_ids": list(criteria_ids),
        "actions": {"total": sum(by_tool.values()), "by_tool": by_tool},
        "questions": questions,
        "precondition_errors": {"exploration": 0},
        "approvals": [],
        "output_error": error,
        "status": "completed" if output is not None else "output_invalid",
    }


@dataclass
class TrialMonitor:
    """Injeção planejada observada pelo supervisor, nunca pedida ao agente."""

    kind: str | None
    after_actions: int
    cancel: threading.Event
    history: Callable[[], tuple[HistoryStore, str] | None]
    observed: bool = False
    _stop: threading.Event = field(default_factory=threading.Event)

    def _loop(self) -> None:
        while not self._stop.wait(0.25):
            bound = self.history()
            if bound is None or self.kind is None:
                continue
            history, run_id = bound
            with contextlib.suppress(Exception):
                state = history.execution_state(run_id)
                if state.actions_completed >= self.after_actions:
                    self.observed = True
                    if self.kind == "interrupt":
                        history.request_cancellation(run_id, origin="eval")
                        self.cancel.set()
                    else:
                        history.latch_stop(run_id, "blocked", "eval_scenario_policy", origin="eval")
                    return

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._loop, name="eval-injection", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()


class SuiteRunner:
    def __init__(self, deps: RunnerDeps) -> None:
        self.deps = deps
        self.repo = deps.history.evaluations
        self.durations: list[float] = []
        self.current_attempt: str | None = None

    # Ciclo da suite --------------------------------------------------------------------------

    def run(self, suite_id: str, *, resume: bool = False) -> SuiteOutcome:
        suite = self.repo.get_suite(suite_id)
        if suite.status in {SuiteStatus.COMPLETED.value, SuiteStatus.CANCELLED.value}:
            return SuiteOutcome(suite, 0, "suite already finished")
        if resume:
            self._check_identity(suite)
        lease = self.repo.acquire_lease(suite_id, owner_identity(), owner_gone=self.deps.owner_gone)
        beat = Heartbeat(self.repo, lease, self.deps.heartbeat_interval)
        beat.start()
        try:
            self._recover(lease)
            self.repo.set_suite_status(lease, suite_id, SuiteStatus.RUNNING.value)
            spec = spec_from_manifest(suite.manifest)
            outcome = self._explore(lease, beat, suite, spec)
            if outcome is not None:
                return outcome
            from lastro.evaluation.judge import JudgeService

            if self.deps.judge is not None:
                try:
                    JudgeService(
                        self.deps.history, self.deps.judge, self.deps.project_dir
                    ).judge_suite(suite_id)
                except EvalError as exc:
                    if exc.code in {"rate_limit", "usage_cap", "suite_cost_cap"}:
                        paused = self.repo.set_suite_status(
                            lease, suite_id, SuiteStatus.PAUSED.value, f"judge:{exc.code}"
                        )
                        return SuiteOutcome(paused, 4, exc.code)
                    raise
            return self._finish(lease, suite_id)
        except KeyboardInterrupt:
            # O slot interrompido recomeça no resume com nova tentativa e novo reset.
            with contextlib.suppress(Exception):
                if self.current_attempt is not None:
                    self.repo.finish_attempt(
                        lease,
                        self.current_attempt,
                        status=AttemptStatus.PAUSED.value,
                        reason="interrupted",
                        trial_status=TrialStatus.PAUSED.value,
                    )
            with contextlib.suppress(Exception):
                self.repo.set_suite_status(lease, suite_id, SuiteStatus.PAUSED.value, "interrupted")
            return SuiteOutcome(self.repo.get_suite(suite_id), 130, "interrupted")
        finally:
            beat.stop()
            with contextlib.suppress(Exception):
                self.repo.release_lease(lease)

    def _finish(self, lease: Lease, suite_id: str) -> SuiteOutcome:
        from lastro.evaluation.metrics import recompute
        from lastro.evaluation.review import ensure_sample

        sample = ensure_sample(self.deps.history, suite_id)
        recompute(self.deps.history, suite_id, self.deps.project_dir)
        reviewed = sample.complete
        status = SuiteStatus.COMPLETED if reviewed else SuiteStatus.AWAITING_REVIEW
        final = self.repo.set_suite_status(lease, suite_id, status.value)
        return SuiteOutcome(final, 0)

    def _check_identity(self, suite: SuiteRecord) -> None:
        """Retomada não muda executor, versão, modelo fixado, catálogo nem rubrica."""
        identity = self.deps.executor.identity()
        if identity.name != suite.executor_name or identity.version != suite.executor_version:
            raise EvalError("resume_identity_changed", "executor or version changed since start")
        if hashlib.sha256(catalog_bytes()).hexdigest() != suite.catalog_sha256:
            raise EvalError("snapshot_changed", "catalog changed since start")
        if sha256_text(rubric_text()) != suite.rubric_sha256:
            raise EvalError("snapshot_changed", "judge rubric changed since start")
        requested = suite.model_identity.get("requested")
        if requested is not None and identity.model_requested not in (None, requested):
            raise EvalError("resume_identity_changed", "pinned model changed since start")

    def _recover(self, lease: Lease) -> None:
        """Tentativa órfã de um supervisor morto vira inválida; nunca retoma cliques antigos.

        Reserva aberta de um supervisor morto vira `uncertain`: a cobrança pode ter acontecido e
        a verba nunca é recuperada no resume.
        """
        for entry in self.repo.list_budget(lease.suite_id):
            if entry.status in {"reserved", "sent"}:
                self.repo.update_budget(lease.suite_id, entry.request_key, "uncertain")
        for attempt in self.repo.interrupted_attempts(lease.suite_id):
            self.repo.finish_attempt(
                lease,
                attempt.id,
                status=AttemptStatus.INVALID.value,
                reason=InvalidReason.CRASH.value,
                trial_status=TrialStatus.INVALID.value,
            )

    def _explore(
        self, lease: Lease, beat: Heartbeat, suite: SuiteRecord, spec: SuiteSpec
    ) -> SuiteOutcome | None:
        trials = self.repo.list_trials(suite.id)
        measured_total = sum(
            1
            for item in trials
            if item.role == TrialRole.MEASURED.value
            and item.status != TrialStatus.NOT_APPLICABLE.value
        )
        try:
            for trial in trials:
                if trial.status not in {TrialStatus.PENDING.value, TrialStatus.PAUSED.value}:
                    continue
                if beat.lost.is_set() or not self.repo.owns(lease):
                    raise EvalStoreError("lease_lost", "runner lost the suite lease")
                done = sum(
                    1
                    for item in self.repo.list_trials(suite.id)
                    if item.role == TrialRole.MEASURED.value
                    and item.status in {TrialStatus.VALID.value, TrialStatus.INVALID.value}
                )
                self.deps.progress(
                    Progress(
                        done + (1 if trial.role == TrialRole.MEASURED.value else 0),
                        measured_total,
                        trial.condition,
                        trial.repetition,
                        spec.runs_per_condition,
                        self._eta(measured_total - done),
                        "running",
                    )
                )
                started = self.deps.monotonic()
                result = self._trial(lease, suite, spec, trial)
                if result is not None and result.status.pauses_suite:
                    paused = self.repo.set_suite_status(
                        lease, suite.id, SuiteStatus.PAUSED.value, result.status.value
                    )
                    return SuiteOutcome(paused, 4, result.status.value)
                if trial.role == TrialRole.MEASURED.value:
                    self.durations.append(self.deps.monotonic() - started)
        finally:
            self.deps.demo.stop()
        return None

    def _eta(self, remaining: int) -> float | None:
        if not self.durations:
            return None
        return remaining * (sum(self.durations) / len(self.durations))

    # Trial -----------------------------------------------------------------------------------

    def _invalid(self, lease: Lease, attempt: AttemptRecord, reason: InvalidReason) -> None:
        self.repo.finish_attempt(
            lease,
            attempt.id,
            status=AttemptStatus.INVALID.value,
            reason=reason.value,
            trial_status=TrialStatus.INVALID.value,
        )

    def _trial(
        self, lease: Lease, suite: SuiteRecord, spec: SuiteSpec, trial: TrialRecord
    ) -> HeadlessResult | None:
        deps = self.deps
        identity = deps.executor.identity()
        attempt = self.repo.start_attempt(
            lease,
            trial.id,
            history_relative_template(suite.id, trial.id),
            identity.as_json(),
        )
        self.current_attempt = attempt.id
        paths = trial_paths(deps.project_dir, suite.id, trial.id, attempt.attempt)
        scenario = next(item for item in spec.scenarios if item.id == trial.scenario)
        mission = spec.mission(scenario.mission)
        mission_text = suite.manifest["mission_texts"][mission.id]
        slot = next(item for item in suite.manifest["slots"] if item["ordinal"] == trial.ordinal)
        # Reset e versão vêm antes de qualquer login; posse verificada antes do reset.
        if not self.repo.owns(lease):
            raise EvalStoreError("lease_lost", "runner lost the suite lease")
        deps.demo.ensure(int(slot["demo_seed"]))
        reset = deps.demo.reset(scenario.demo)
        self.repo.update_attempt(
            lease, attempt.id, reset_json=reset.payload or {"error": reset.error}
        )
        if not reset.ok:
            self._invalid(lease, attempt, InvalidReason.RESET)
            return None
        try:
            version = deps.demo.version()
            self.repo.update_attempt(lease, attempt.id, version_json=version)
            from lastro_demo import __version__ as demo_version

            check_version(version, scenario.demo, demo_version)
        except EvalError:
            self._invalid(lease, attempt, InvalidReason.VERSION)
            return None
        condition = Condition(trial.condition)
        effective = conditions.EffectiveCondition.of(condition)
        bundle = conditions.derive_bundle(effective)
        binding = (
            conditions.binding_block(
                effective,
                suite_id=suite.id,
                trial_id=trial.id,
                bundle_sha256=bundle.sha256 if bundle else None,
            )
            if effective.lastro
            else None
        )
        request = WorkspaceRequest(
            project_id=deps.project_id,
            executor=suite.executor_name,
            effective=effective,
            bundle=bundle,
            mission_text=mission_text,
            criteria_ids=tuple(item.id for item in mission.criteria),
            demo_url=deps.demo.url,
            tenant_id="boreal" if scenario.tenant_mismatch else DEFAULT_TENANT[0],
            tenant_display="Clínica Boreal" if scenario.tenant_mismatch else DEFAULT_TENANT[1],
            max_minutes=spec.limits.max_minutes or 30,
            max_actions=spec.limits.max_actions or 150,
        )
        try:
            prepare_workspace(paths, request)
            memory_sha = self._memory(trial, effective, scenario.memory.mode.value, paths)
        except EvalError:
            self._invalid(lease, attempt, InvalidReason.ISOLATION)
            return None
        if not effective.lastro:
            self._prepare_baseline(paths, deps.demo.url, request.max_actions)
        cancel = threading.Event()
        bound: dict[str, Any] = {}

        def on_run(run_id: str) -> None:
            from lastro.config.loader import load_project
            from lastro.history import open_history

            workspace = paths.workspace
            project = load_project(workspace / "lastro.toml", "eval", cwd=workspace)
            bound["history"] = open_history(project)
            bound["run_id"] = run_id
            self.repo.update_attempt(lease, attempt.id, run_id=run_id)

        def trial_history() -> tuple[HistoryStore, str] | None:
            if "history" not in bound:
                return None
            return bound["history"], bound["run_id"]

        decisions: DecisionLoop | None = None
        monitor = TrialMonitor(
            scenario.injection.kind if scenario.injection else None,
            scenario.injection.after_actions if scenario.injection else 0,
            cancel,
            trial_history,
        )
        access_start = deps.demo.log_offset()
        self.repo.set_trial_status(lease, trial.id, TrialStatus.RUNNING.value)
        launch = TrialLaunch(
            task=task_for(trial.id, suite.executor_name, spec.model, paths.workspace, binding),
            paths=paths,
            effective=effective,
            bundle=bundle,
            binding=binding,
            credentials=deps.credentials,
            max_minutes=request.max_minutes,
            max_actions=request.max_actions,
            system_identity=system_identity(version, trial.variant),
            on_run=on_run,
        )
        if effective.lastro:
            trial_decisions = TrialDecisions(
                scenario.approver,
                scenario.clarifications,
                lambda: bound.get("history"),
                GateMode.SCRIPTED,
            )
            decisions = DecisionLoop(trial_decisions.tick, sleep=deps.decision_sleep)
            decisions.start()
        component = "warmup" if trial.role == TrialRole.WARMUP.value else "exploration"
        budget_key = f"{component}:{attempt.id}"
        reserved = NATIVE_RESERVATION_NUSD if suite.executor_name == "native" else 0
        cap = usd_nusd(spec.limits.suite_budget_usd)
        entry = self.repo.reserve_budget(suite.id, component, budget_key, reserved, cap, attempt.id)
        if entry is None:
            if decisions is not None:
                decisions.close()
            self.repo.finish_attempt(
                lease,
                attempt.id,
                status=AttemptStatus.PAUSED.value,
                reason="suite_cost_cap",
                trial_status=TrialStatus.PAUSED.value,
            )
            return HeadlessResult(status=StopReason.COST_CAP, executor=identity)
        self.repo.update_budget(suite.id, budget_key, "sent")
        monitor.start()
        started = deps.monotonic()
        result: HeadlessResult | None = None
        crashed = False
        try:
            result = deps.executor.launch(launch, cancel)
        except EvalError as exc:
            if exc.code == "isolation_unavailable":
                self._invalid(lease, attempt, InvalidReason.ISOLATION)
                return None
            if exc.code in {"rate_limit", "usage_cap", "suite_cost_cap"}:
                reason = (
                    StopReason.COST_CAP if exc.code == "suite_cost_cap" else StopReason(exc.code)
                )
                result = HeadlessResult(status=reason, executor=identity)
            else:
                crashed = True
        except Exception:
            crashed = True
        finally:
            monitor.stop()
            if decisions is not None:
                decisions.close()
        duration = deps.monotonic() - started
        self._settle(suite, budget_key, result, attempt.id)
        access_end = deps.demo.log_offset()
        window = deps.demo.window(access_start, access_end)
        self.repo.update_attempt(
            lease,
            attempt.id,
            access_start=access_start,
            access_end=access_end,
            access_sha256=window.sha256,
            usage_json=(result.usage.as_json() if result else None),
            observed_cost_nusd=(result.usage.cost_nusd if result else None),
            logs_relative_path=paths.relative(paths.logs),
            executor_identity_json=(result.executor.as_json() if result else identity.as_json()),
        )
        if crashed or result is None or result.status == StopReason.CRASH:
            self._invalid(lease, attempt, InvalidReason.CRASH)
            return result
        if result.status.pauses_suite:
            self.repo.finish_attempt(
                lease,
                attempt.id,
                status=AttemptStatus.PAUSED.value,
                reason=result.status.value,
                trial_status=TrialStatus.PAUSED.value,
            )
            return result
        snapshot = self._snapshot(
            paths,
            trial,
            effective,
            result,
            bound,
            mission.criteria,
            window,
            duration,
            memory_sha,
            monitor.observed,
            scenario,
        )
        data = json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode("utf-8")
        write_bytes_atomically(paths.snapshot, data)
        self.repo.finish_attempt(
            lease,
            attempt.id,
            status=AttemptStatus.VALID.value,
            reason=result.status.value,
            trial_status=TrialStatus.VALID.value,
            snapshot_relative_path=paths.relative(paths.snapshot),
            snapshot_sha256=hashlib.sha256(data).hexdigest(),
            select=True,
        )
        return result

    def _settle(
        self, suite: SuiteRecord, key: str, result: HeadlessResult | None, attempt_id: str
    ) -> None:
        """Custo observado liquida a reserva; sem resultado, a cobrança fica incerta."""
        if result is None:
            self.repo.update_budget(suite.id, key, "uncertain")
            return
        # O ledger nativo já contabilizou a chamada: o registro aponta para ele, sem somar de novo.
        source = f"native-ledger:{result.run_ref}" if suite.executor_name == "native" else None
        self.repo.update_budget(
            suite.id, key, "settled", actual_nusd=result.usage.cost_nusd, source_ref=source
        )
        if result.reproduction_usage is not None:
            child_key = f"reproduction:{attempt_id}"
            cap = usd_nusd(spec_from_manifest(suite.manifest).limits.suite_budget_usd)
            self.repo.reserve_budget(suite.id, "reproduction", child_key, 0, cap, attempt_id)
            self.repo.update_budget(
                suite.id,
                child_key,
                "settled",
                actual_nusd=result.reproduction_usage.cost_nusd,
                source_ref=source,
            )

    def _memory(
        self,
        trial: TrialRecord,
        effective: conditions.EffectiveCondition,
        mode: str,
        paths: Any,
    ) -> str | None:
        """Pares warm herdam só o conhecimento do aquecimento da própria linhagem."""
        if mode != "paired" or trial.parent_trial_id is None or not effective.memory:
            return None
        parent = self.repo.get_trial(trial.parent_trial_id)
        if parent.status != TrialStatus.VALID.value or parent.selected_attempt_id is None:
            return None
        source = self.repo.get_attempt(parent.selected_attempt_id)
        source_data = self.deps.project_dir / source.history_relative_path
        return copy_memory(source_data, paths.data, self.deps.project_id)

    def _prepare_baseline(self, paths: Any, demo_url: str, max_actions: int) -> None:
        from lastro.evaluation.baseline import CONTEXT_FILE

        state = paths.root / "baseline-storage.json"
        if self.deps.baseline_login is not None:
            self.deps.baseline_login(demo_url, state)
        (paths.root / CONTEXT_FILE).write_text(
            json.dumps(
                {
                    "demo_url": demo_url,
                    "storage_state": state.name,
                    "max_actions": max_actions,
                }
            ),
            encoding="utf-8",
        )

    def _snapshot(
        self,
        paths: Any,
        trial: TrialRecord,
        effective: conditions.EffectiveCondition,
        result: HeadlessResult,
        bound: dict[str, Any],
        criteria: Any,
        window: Any,
        duration: float,
        memory_sha: str | None,
        injection_observed: bool,
        scenario: Any,
    ) -> dict[str, Any]:
        criteria_ids = tuple(item.id for item in criteria)
        if effective.lastro and "history" in bound:
            collected = collect_lastro(bound["history"], bound["run_id"], criteria_ids)
            stale_misuse = None
        else:
            collected = collect_baseline(paths.root, criteria_ids)
            stale_misuse = None
        from lastro.evaluation.metrics import access_summary

        return {
            "schema_version": 1,
            "trial_id": trial.id,
            "condition": trial.condition,
            "scenario": trial.scenario,
            "variant": trial.variant,
            "role": trial.role,
            "run_ref": result.run_ref,
            "stop_reason": result.status.value,
            "executor": result.executor.as_json(),
            "usage": result.usage.as_json(),
            "duration_seconds": round(duration, 3),
            "memory": {"sha256": memory_sha, "stale_misuse": stale_misuse},
            "injection": {
                "kind": scenario.injection.kind if scenario.injection else None,
                "observed": injection_observed,
            },
            "access": access_summary(window, expected_tenant=DEFAULT_TENANT[0]),
            **collected,
        }


def suite_dir(project_dir: Path, suite_id: str) -> Path:
    return suite_root(project_dir, suite_id)


NAMESPACES = {"retrieval": "retrieval.", "autonomous": "autonomous."}


def persist_task_metrics(
    repo: EvalRepository,
    lease: Lease,
    trial_id: str,
    task: Any,
    executor: dict[str, Any],
    measurements: list[Any],
) -> int:
    """Resultados de tarefas sem browser no mesmo ciclo de lease, tentativa e revisão.

    Cada consumidor só grava no próprio namespace (`retrieval.*`, `autonomous.*`); métricas da
    exploração do demo nunca são produzidas por esses propósitos.
    """
    from lastro.history.evaluation import ResultInput

    prefix = NAMESPACES.get(task.purpose.value)
    if prefix is None:
        raise EvalError("invalid_task", "only retrieval and autonomous tasks use this port")
    if any(not item.metric.startswith(prefix) for item in measurements):
        raise EvalError("invalid_task", f"metrics must use the {prefix}* namespace")
    trial = repo.get_trial(trial_id)
    attempt = repo.start_attempt(
        lease, trial_id, f"evals/{lease.suite_id}/tasks/{{attempt}}", executor
    )
    repo.finish_attempt(
        lease,
        attempt.id,
        status=AttemptStatus.VALID.value,
        reason=StopReason.COMPLETED.value,
        trial_status=TrialStatus.VALID.value,
        select=True,
    )
    stratum = {
        "purpose": task.purpose.value,
        "executor": executor.get("name"),
        "executor_version": executor.get("version"),
        "condition": trial.condition,
        "scenario": trial.scenario,
        "variant": trial.variant,
    }
    from lastro.evaluation.metrics import stratum_key

    key = stratum_key(stratum)
    inputs = hashlib.sha256(json.dumps([m.metric for m in measurements]).encode()).hexdigest()
    results = [
        ResultInput(
            stratum=stratum,
            stratum_key=key,
            metric=item.metric,
            unit=item.unit,
            n=1 if item.value is not None else 0,
            planned_n=1,
            invalid_n=0,
            excluded_n=0 if item.value is not None else 1,
            mean=item.value,
            sample_stddev=None,
            complete=item.value is not None,
            limitations=()
            if item.value is not None
            else (f"unavailable:{item.unavailable_reason}",),
            inputs_sha256=inputs,
        )
        for item in measurements
    ]
    return repo.save_revision(
        lease.suite_id, [(attempt.id, item) for item in measurements], results
    )
