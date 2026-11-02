"""Fachada do histórico consumida pela CLI e pelas demais features.

Nenhuma conexão SQLite nem SQL atravessa esta fronteira: entradas e saídas são DTOs tipados.
"""

import contextlib
import os
import secrets
import shutil
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from lastro import get_version
from lastro.config.loader import LoadedConfig, ProjectContext
from lastro.history.artifacts import (
    StoredFile,
    insert_artifact,
    list_artifacts,
    parse_artifact_type,
    store_file,
)
from lastro.history.backup import BackupResult, create_backup
from lastro.history.created_records import (
    creation_event,
    insert_created_record,
    mark_cleaned,
    select_created_records,
)
from lastro.history.database import Database, RetryPolicy, db_timestamp
from lastro.history.errors import (
    ActiveRunExistsError,
    DiskFullError,
    HistoryError,
    HistoryUnavailableError,
    InvalidEventError,
    InvalidTransitionError,
    MissionPlanError,
    RunNotActiveError,
)
from lastro.history.events import (
    PreparedEvent,
    insert_event,
    prepare_event,
    select_events,
    write_fallback,
)
from lastro.history.layout import MISSION_SNAPSHOT_RELATIVE, HistoryLayout
from lastro.history.migrations import apply_migrations
from lastro.history.mission_plans import (
    INVALID_PLAN,
    INVALID_REFERENCE,
    answer_clarification,
    blocking_gap,
    mission_content,
    request_clarification,
    resolve_actions,
    select_active_plan,
    select_clarifications,
    select_plans,
    submit_plan,
)
from lastro.history.models import (
    LIFECYCLE_EVENTS,
    PROCESS_LOST_REASON,
    ActionReference,
    ArtifactRecord,
    ArtifactType,
    Clarification,
    CleanupResult,
    CleanupStatus,
    CreatedRecord,
    CreatedRecordInput,
    CreatedRun,
    CriterionResult,
    CriterionResultDraft,
    CriterionSubmission,
    EventRecord,
    EventType,
    ExecutorInfo,
    Finding,
    FindingDraft,
    FindingSubmission,
    HeuristicCheck,
    JsonObject,
    JsonValue,
    Mission,
    MissionContext,
    MissionPlan,
    MissionSource,
    PlanStatus,
    PlanSubmission,
    PolicyDecision,
    ReportInputs,
    ReportKind,
    ReportSnapshot,
    ReproductionAttempt,
    ReproductionOutcome,
    RunFilter,
    RunPaths,
    RunRecord,
    RunStatus,
    RunSummary,
    SnapshotStatus,
)
from lastro.history.oracles import record_signal, select_signals
from lastro.history.processes import ProcessChecker, process_exists
from lastro.history.queries import select_runs
from lastro.history.reporting import (
    check_criterion_result,
    check_finding,
    close_snapshot,
    insert_criterion_result,
    insert_finding,
    insert_reproduction_attempt,
    open_snapshot,
    promote,
    seal_results,
    select_criterion_results,
    select_finding,
    select_findings,
    select_snapshots,
)
from lastro.history.runs import (
    RUN_ID_ENTROPY_BYTES,
    apply_transition,
    clear_active_pointer,
    config_snapshot,
    insert_run,
    new_run_id,
    publish_active_pointer,
    read_active_pointer,
    select_non_terminal,
    select_run,
    validate_mission,
)
from lastro.mission.charter import DEFAULT_MAX_TIMEBOX_MINUTES, build_charter
from lastro.mission.models import (
    ClarificationAnswer,
    ClarificationInput,
    HeuristicCheckInput,
    MissionPlanInput,
)
from lastro.oracles.models import OracleOccurrence, OracleSignal
from lastro.oracles.sanitize import normalize
from lastro.timeutil import utc_now

type Clock = Callable[[], datetime]
type Entropy = Callable[[int], str]

MAX_RUN_ID_ATTEMPTS = 20
# Depois de esgotar o retry, a marcação de blocked é uma última tentativa única.
_LAST_ATTEMPT_POLICY = RetryPolicy(delays=())


def _run_exists(conn: sqlite3.Connection, run_id: str) -> bool:
    return conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone() is not None


class HistoryStore:
    """Registro de runs, event log e artifact registry de um projeto."""

    def __init__(
        self,
        layout: HistoryLayout,
        database: Database,
        *,
        project_id: str,
        timezone: str,
        clock: Clock = utc_now,
        entropy: Entropy = secrets.token_hex,
        process_checker: ProcessChecker = process_exists,
        owner_pid: int | None = None,
    ) -> None:
        self._layout = layout
        self._database = database
        self._clock = clock
        self._entropy = entropy
        self._process_checker = process_checker
        self._failed_closed: set[str] = set()
        self.project_id = project_id
        self.timezone = timezone
        self.owner_pid = owner_pid if owner_pid is not None else os.getpid()

    @property
    def project_dir(self) -> Path:
        return self._layout.root

    @property
    def database_path(self) -> Path:
        return self._layout.database

    def _now(self) -> str:
        return db_timestamp(self._clock())

    def run_dir(self, run_id: str) -> Path:
        """Pasta privada da run, sem consultar o banco."""
        return self._layout.run_dir(run_id)

    def run_paths(self, run_id: str) -> RunPaths:
        record = self.get_run(run_id)
        snapshot = (
            self._layout.mission_snapshot(run_id)
            if record.mission_source == MissionSource.FILE
            else None
        )
        return RunPaths(
            run_dir=self._layout.run_dir(run_id),
            evidence_dir=self._layout.evidence_dir(run_id),
            mission_snapshot=snapshot,
        )

    def create_run(
        self,
        context: ProjectContext,
        mission: Mission,
        executor: ExecutorInfo | None = None,
        *,
        skill_bundle_version: str | None = None,
        analyzed_code_version: str | None = None,
        executed_system_version: str | None = None,
    ) -> CreatedRun:
        """Cria e confirma a run, a missão e run_started antes de publicar o ponteiro ativo.

        Ao retornar, a run já é durável; só então qualquer interação com o sistema alvo pode
        começar.
        """
        validated = validate_mission(mission)
        active = self.get_active_run()
        if active is not None:
            raise ActiveRunExistsError(active.id)
        executor = executor or ExecutorInfo(name=context.config.executor)
        moment = self._clock()
        run_id, run_dir = self._reserve_run_dir(moment)
        try:
            self._layout.evidence_dir(run_id).mkdir()
            stored: StoredFile | None = None
            if validated.file_bytes is not None:
                stored = store_file(
                    run_dir, MISSION_SNAPSHOT_RELATIVE, content=validated.file_bytes
                )
            started_at = db_timestamp(moment)
            record = RunRecord(
                id=run_id,
                project_id=self.project_id,
                profile=context.profile_name,
                environment_url=context.profile.target_url,
                tenant=context.profile.tenant_id,
                mission_source=validated.source,
                mission_text=validated.text,
                mission_snapshot_path=stored.relative_path if stored else None,
                mission_file_sha256=stored.sha256 if stored else None,
                issue_snapshot_id=validated.issue_snapshot_id,
                started_at=started_at,
                ended_at=None,
                status=RunStatus.RUNNING,
                status_reason=None,
                owner_pid=self.owner_pid,
                executor_name=executor.name,
                executor_version=executor.version,
                lastro_version=get_version(),
                skill_bundle_version=skill_bundle_version,
                config_snapshot=config_snapshot(context),
                analyzed_code_version=analyzed_code_version,
                executed_system_version=executed_system_version,
            )
            started = prepare_event(
                EventType.RUN_STARTED,
                payload={
                    "profile": record.profile,
                    "mission_source": record.mission_source.value,
                    "executor": record.executor_name,
                },
            )

            def persist(conn: sqlite3.Connection) -> RunRecord:
                insert_run(conn, record)
                if stored is not None:
                    insert_artifact(conn, run_id, stored, ArtifactType.SNAPSHOT, started_at)
                insert_event(conn, run_id, started, started_at)
                return select_run(conn, run_id)

            confirmed = self._database.write(persist)
        except BaseException:
            shutil.rmtree(run_dir, ignore_errors=True)
            raise
        publish_active_pointer(self._layout.active_run, confirmed)
        return CreatedRun(record=confirmed, paths=self.run_paths(run_id))

    def _reserve_run_dir(self, moment: datetime) -> tuple[str, Path]:
        self._layout.runs_dir.mkdir(parents=True, exist_ok=True)
        for _ in range(MAX_RUN_ID_ATTEMPTS):
            run_id = new_run_id(moment, self.timezone, self._entropy(RUN_ID_ENTROPY_BYTES))
            run_dir = self._layout.run_dir(run_id)
            try:
                run_dir.mkdir()
            except FileExistsError:
                continue
            if not self._database.read(lambda conn, run_id=run_id: _run_exists(conn, run_id)):
                return run_id, run_dir
            run_dir.rmdir()
        raise HistoryError("não foi possível gerar um identificador de run único")

    def get_run(self, run_id: str) -> RunRecord:
        return self._database.read(lambda conn: select_run(conn, run_id))

    def get_active_run(self, run_id: str | None = None) -> RunRecord | None:
        """Run ativa somente quando ponteiro, banco e processo dono são coerentes."""
        pointer = read_active_pointer(self._layout.active_run)
        if pointer is None or (run_id is not None and pointer.run_id != run_id):
            return None
        try:
            record = self.get_run(pointer.run_id)
        except HistoryError:
            return None
        if record.status.is_terminal or record.owner_pid != pointer.owner_pid:
            return None
        if not self._process_checker(record.owner_pid):
            return None
        return record

    def transition_run(
        self, run_id: str, status: RunStatus | str, reason: str | None = None
    ) -> RunRecord:
        target = RunStatus(status)
        timestamp = self._now()
        record = self._database.write(
            lambda conn: apply_transition(conn, run_id, target, reason, timestamp)
        )
        if record.status.is_terminal:
            clear_active_pointer(self._layout.active_run, run_id)
        return record

    def finish_run(
        self, run_id: str, status: RunStatus | str, reason: str | None = None
    ) -> RunRecord:
        target = RunStatus(status)
        if not target.is_terminal:
            raise InvalidTransitionError(f"{target.value} não é um estado terminal")
        return self.transition_run(run_id, target, reason)

    def reconcile_orphans(self) -> list[str]:
        """Marca como interrupted toda run não terminal cujo processo dono não existe mais."""
        timestamp = self._now()

        def reconcile(conn: sqlite3.Connection) -> list[str]:
            lost: list[str] = []
            for record in select_non_terminal(conn):
                if record.owner_pid == self.owner_pid or self._process_checker(record.owner_pid):
                    continue
                apply_transition(
                    conn, record.id, RunStatus.INTERRUPTED, PROCESS_LOST_REASON, timestamp
                )
                lost.append(record.id)
            return lost

        lost = self._database.write(reconcile)
        for run_id in lost:
            clear_active_pointer(self._layout.active_run, run_id)
        return lost

    def append_event(
        self,
        run_id: str,
        event_type: EventType | str,
        *,
        payload: object = None,
        result: object = None,
        tool_name: str | None = None,
        arguments: object = None,
        policy_decision: PolicyDecision | str | None = None,
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> EventRecord:
        """Redige, grava e confirma um evento antes de retornar, sem buffering.

        Com o banco indisponível após o retry, grava fallback redigido, tenta marcar a run como
        blocked e passa a recusar novos eventos dessa run neste processo.
        """
        if run_id in self._failed_closed:
            raise HistoryUnavailableError()
        event = prepare_event(
            event_type,
            payload=payload,
            result=result,
            tool_name=tool_name,
            arguments=arguments,
            policy_decision=policy_decision,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        if event.event_type in LIFECYCLE_EVENTS:
            raise InvalidEventError(f"{event.event_type.value} é gravado apenas pelo registro")
        timestamp = self._now()

        def append(conn: sqlite3.Connection) -> EventRecord:
            record = select_run(conn, run_id)
            if record.status.is_terminal:
                raise RunNotActiveError(run_id, record.status.value)
            return insert_event(conn, run_id, event, timestamp)

        try:
            return self._database.write(append)
        except HistoryUnavailableError:
            self._fail_closed(run_id, event, timestamp)
            raise

    def _fail_closed(self, run_id: str, event: PreparedEvent, timestamp: str) -> None:
        self._failed_closed.add(run_id)
        with contextlib.suppress(OSError):
            write_fallback(
                self._layout.fallback(run_id),
                event.fallback_entry(run_id, timestamp, "history_unavailable"),
            )
        self._try_block(run_id, str(HistoryUnavailableError()))

    def _try_block(self, run_id: str, reason: str) -> None:
        last_attempt = Database(self._database.path, _LAST_ATTEMPT_POLICY)
        timestamp = self._now()
        with contextlib.suppress(HistoryError, sqlite3.Error):
            last_attempt.write(
                lambda conn: apply_transition(conn, run_id, RunStatus.BLOCKED, reason, timestamp)
            )
            clear_active_pointer(self._layout.active_run, run_id)

    def list_events(
        self, run_id: str, after_sequence: int = 0, limit: int | None = None
    ) -> list[EventRecord]:
        """Eventos em ordem de sequência depois do cursor; cada chamada abre e fecha a leitura."""
        return self._database.read(lambda conn: select_events(conn, run_id, after_sequence, limit))

    def record_oracle_signal(self, run_id: str, occurrence: OracleOccurrence) -> OracleSignal:
        if run_id in self._failed_closed:
            raise HistoryUnavailableError()
        normalized = normalize(occurrence)
        try:
            return self._database.write(lambda conn: record_signal(conn, run_id, normalized))
        except HistoryUnavailableError:
            event = prepare_event(EventType.ORACLE_SIGNAL, payload=normalized.model_dump())
            self._fail_closed(run_id, event, normalized.timestamp)
            raise

    def list_oracle_signals(self, run_id: str, limit: int | None = None) -> list[OracleSignal]:
        if run_id in self._failed_closed:
            raise HistoryUnavailableError()
        try:
            return self._database.read(lambda conn: select_signals(conn, run_id, limit))
        except HistoryUnavailableError:
            event = prepare_event(EventType.ORACLE_SIGNAL, payload={"operation": "list"})
            self._fail_closed(run_id, event, self._now())
            raise

    def record_created(self, data: CreatedRecordInput) -> CreatedRecord:
        """Confirma registro e record_created na mesma transação, ou nenhum dos dois."""
        if data.run_id in self._failed_closed:
            raise HistoryUnavailableError()
        timestamp = self._now()
        try:
            return self._database.write(
                lambda conn: insert_created_record(conn, self.project_id, data, timestamp)
            )
        except HistoryUnavailableError:
            self._fail_closed(data.run_id, creation_event(data), timestamp)
            raise

    def list_created_records(
        self,
        *,
        run_id: str | None = None,
        cleanup_status: CleanupStatus | None = None,
        profile: str | None = None,
    ) -> list[CreatedRecord]:
        """Registros do projeto em ordem de criação, contrato de leitura do relatório."""
        return self._database.read(
            lambda conn: select_created_records(
                conn,
                self.project_id,
                run_id=run_id,
                cleanup_status=cleanup_status,
                profile=profile,
            )
        )

    def mark_created_record_cleaned(self, tracking_id: str) -> CleanupResult:
        """Registra a limpeza manual feita fora do Lastro; nada é enviado ao sistema alvo."""
        timestamp = self._now()
        return self._database.write(
            lambda conn: mark_cleaned(conn, self.project_id, tracking_id, timestamp)
        )

    def mission_context(self, run_id: str) -> MissionContext:
        """Missão durável da run; a de arquivo é lida do snapshot privado e conferida."""
        record = self.get_run(run_id)
        snapshot = (
            self._layout.mission_snapshot(run_id)
            if record.mission_source == MissionSource.FILE
            else None
        )
        content, digest = mission_content(record, snapshot)
        active = self.get_active_mission_plan(run_id)
        if active is None:
            status = PlanStatus.MISSING
        else:
            status = PlanStatus.ACTIVE if active.exploration_allowed else PlanStatus.BLOCKED
        return MissionContext(
            run_id=run_id,
            source=record.mission_source,
            content=content,
            content_sha256=digest,
            skill_bundle_sha256=record.skill_bundle_version,
            plan_status=status,
        )

    def submit_mission_plan(
        self,
        run_id: str,
        plan: MissionPlanInput,
        *,
        max_minutes: int = DEFAULT_MAX_TIMEBOX_MINUTES,
    ) -> PlanSubmission:
        """Confirma critérios e revisão do plano juntos; submissão idêntica é idempotente."""
        if run_id in self._failed_closed:
            raise HistoryUnavailableError()
        try:
            charter = build_charter(plan.charter, max_minutes)
        except ValueError as exc:
            raise MissionPlanError(INVALID_PLAN, str(exc)) from None
        timestamp = self._now()
        try:
            return self._database.write(
                lambda conn: submit_plan(conn, run_id, plan, charter, timestamp)
            )
        except HistoryUnavailableError:
            event = prepare_event(
                EventType.POLICY_DECISION,
                tool_name="submit_mission_plan",
                policy_decision=PolicyDecision.DENY,
                payload={"reason": "history_unavailable"},
            )
            self._fail_closed(run_id, event, timestamp)
            raise

    def get_active_mission_plan(self, run_id: str) -> MissionPlan | None:
        return self._database.read(lambda conn: select_active_plan(conn, run_id))

    def list_mission_plans(self, run_id: str) -> list[MissionPlan]:
        """Todas as revisões em ordem, preservadas para auditoria."""
        return self._database.read(lambda conn: select_plans(conn, run_id))

    def check_blocking_gap(self, run_id: str, gap_id: str) -> MissionPlan:
        return self._database.read(lambda conn: blocking_gap(conn, run_id, gap_id))

    def request_clarification(
        self, run_id: str, request: ClarificationInput
    ) -> tuple[Clarification, bool]:
        """Grava pergunta, clarification_requested e a espera; repetir o gap é idempotente."""
        timestamp = self._now()
        return self._database.write(
            lambda conn: request_clarification(conn, run_id, request, timestamp)
        )

    def answer_clarification(
        self, clarification_id: str, answer: ClarificationAnswer
    ) -> Clarification:
        """Resposta do canal humano: volta a run para running e nunca grava aprovação."""
        timestamp = self._now()
        return self._database.write(
            lambda conn: answer_clarification(conn, clarification_id, answer, timestamp)
        )

    def list_clarifications(self, run_id: str) -> list[Clarification]:
        return self._database.read(lambda conn: select_clarifications(conn, run_id))

    def validate_heuristic_check(
        self, run_id: str, check: HeuristicCheckInput
    ) -> tuple[ActionReference, ...]:
        """Confirma critério do plano ativo e ações reais permitidas desta run."""

        def validate(conn: sqlite3.Connection) -> tuple[ActionReference, ...]:
            if check.criterion_id is not None:
                active = select_active_plan(conn, run_id)
                known = {criterion.id for criterion in active.criteria} if active else set[str]()
                if check.criterion_id not in known:
                    raise MissionPlanError(
                        INVALID_REFERENCE, "criterion_id must belong to the active plan"
                    )
            return resolve_actions(conn, run_id, check.action_ids)

        return self._database.read(validate)

    def list_heuristic_checks(self, run_id: str) -> list[HeuristicCheck]:
        """Checks aceitos, lidos dos eventos de política de record_test_check."""
        checks: list[HeuristicCheck] = []
        for event in self.list_events(run_id):
            if (
                event.event_type != EventType.POLICY_DECISION
                or event.tool_name != "record_test_check"
                or event.policy_decision != PolicyDecision.ALLOW
                or not isinstance(event.payload, dict)
            ):
                continue
            check = event.payload.get("check")
            if isinstance(check, dict):
                checks.append(_check_from_payload(event.correlation_id or "", check))
        return checks

    def register_artifact(
        self,
        run_id: str,
        relative_path: str,
        artifact_type: ArtifactType | str,
        *,
        content: bytes | None = None,
        source: Path | None = None,
    ) -> ArtifactRecord:
        """Grava o arquivo final, mede tamanho e hash no disco e só então registra a row."""
        kind = parse_artifact_type(artifact_type)
        record = self.get_run(run_id)
        run_dir = self._layout.run_dir(run_id)
        try:
            stored = store_file(run_dir, relative_path, content=content, source=source)
        except DiskFullError as exc:
            if not record.status.is_terminal:
                self._try_block(run_id, str(exc))
            raise
        timestamp = self._now()
        try:
            return self._database.write(
                lambda conn: insert_artifact(conn, run_id, stored, kind, timestamp)
            )
        except BaseException:
            with contextlib.suppress(OSError):
                stored.path.unlink()
            raise

    def list_artifacts(self, run_id: str) -> list[ArtifactRecord]:
        return self._database.read(lambda conn: list_artifacts(conn, run_id))

    def check_finding(self, run_id: str, draft: FindingDraft) -> None:
        """Valida referências antes da auditoria; a gravação repete a validação na transação."""
        self._database.read(lambda conn: check_finding(conn, run_id, draft))

    def report_finding(self, run_id: str, draft: FindingDraft) -> FindingSubmission:
        """Confirma achado, passos, evidências e finding_submitted juntos, ou nenhum deles."""
        if run_id in self._failed_closed:
            raise HistoryUnavailableError()
        timestamp = self._now()
        try:
            return self._database.write(lambda conn: insert_finding(conn, run_id, draft, timestamp))
        except HistoryUnavailableError:
            event = prepare_event(
                EventType.FINDING_SUBMITTED, payload={"title": draft.title, "flow": draft.flow}
            )
            self._fail_closed(run_id, event, timestamp)
            raise

    def get_finding(self, finding_id: str) -> Finding:
        return self._database.read(lambda conn: select_finding(conn, finding_id))

    def list_findings(self, run_id: str) -> list[Finding]:
        """Achados estruturados em ordem de criação, lidos por métricas e comparações."""
        return self._database.read(lambda conn: select_findings(conn, run_id))

    def record_reproduction_attempt(
        self,
        finding_id: str,
        child_run_id: str,
        outcome: ReproductionOutcome,
        evidence_artifact_ids: tuple[str, ...],
        executor: str,
    ) -> ReproductionAttempt:
        """Interface interna da reprodução independente; não é exposta ao agente."""
        timestamp = self._now()
        return self._database.write(
            lambda conn: insert_reproduction_attempt(
                conn, finding_id, child_run_id, outcome, evidence_artifact_ids, executor, timestamp
            )
        )

    def promote_finding(self, finding_id: str) -> Finding:
        """Única via para confirmed: evidência própria e ao menos uma reprodução bem-sucedida."""
        timestamp = self._now()
        return self._database.write(lambda conn: promote(conn, finding_id, timestamp))

    def check_criterion_result(self, run_id: str, draft: CriterionResultDraft) -> None:
        self._database.read(lambda conn: check_criterion_result(conn, run_id, draft))

    def record_criterion_result(
        self, run_id: str, draft: CriterionResultDraft
    ) -> CriterionSubmission:
        timestamp = self._now()
        return self._database.write(
            lambda conn: insert_criterion_result(conn, run_id, draft, timestamp)
        )

    def seal_criterion_results(self, run_id: str) -> list[CriterionResult]:
        """Chamado com a run ainda ativa, antes da transição terminal."""
        timestamp = self._now()
        return self._database.write(lambda conn: seal_results(conn, run_id, timestamp))

    def list_criterion_results(
        self, run_id: str, *, current_only: bool = True
    ) -> list[CriterionResult]:
        return self._database.read(
            lambda conn: select_criterion_results(conn, run_id, current_only=current_only)
        )

    def report_inputs(self, run_id: str) -> ReportInputs:
        """Tudo que o relatório projeta, lido numa única transação de leitura."""

        def read(conn: sqlite3.Connection) -> ReportInputs:
            return ReportInputs(
                run=select_run(conn, run_id),
                events=tuple(select_events(conn, run_id)),
                artifacts=tuple(list_artifacts(conn, run_id)),
                findings=tuple(select_findings(conn, run_id)),
                criterion_results=tuple(select_criterion_results(conn, run_id)),
                plan=select_active_plan(conn, run_id),
                clarifications=tuple(select_clarifications(conn, run_id)),
                created_records=tuple(select_created_records(conn, self.project_id, run_id=run_id)),
                oracle_signals=tuple(
                    sorted(select_signals(conn, run_id), key=lambda signal: signal.last_event_id)
                ),
            )

        return self._database.read(read)

    def open_report_snapshot(
        self,
        run_id: str,
        kind: ReportKind,
        input_sha256: str,
        termination_reason: str | None = None,
    ) -> ReportSnapshot:
        timestamp = self._now()
        return self._database.write(
            lambda conn: open_snapshot(
                conn, run_id, kind, input_sha256, termination_reason, timestamp
            )
        )

    def close_report_snapshot(
        self,
        snapshot_id: str,
        *,
        status: SnapshotStatus,
        report_artifact_id: str | None = None,
        fallback_artifact_id: str | None = None,
        issue_artifact_ids: tuple[str, ...] = (),
        integrity_valid: bool | None = None,
        error_text: str | None = None,
        duration_ms: int | None = None,
    ) -> ReportSnapshot:
        timestamp = self._now()
        return self._database.write(
            lambda conn: close_snapshot(
                conn,
                snapshot_id,
                status=status,
                report_artifact_id=report_artifact_id,
                fallback_artifact_id=fallback_artifact_id,
                issue_artifact_ids=issue_artifact_ids,
                integrity_valid=integrity_valid,
                error_text=error_text,
                duration_ms=duration_ms,
                timestamp=timestamp,
            )
        )

    def list_report_snapshots(
        self, run_id: str, kind: ReportKind | None = None
    ) -> list[ReportSnapshot]:
        return self._database.read(lambda conn: select_snapshots(conn, run_id, kind))

    def backup(self, destination: Path) -> BackupResult:
        return create_backup(
            self._database, self._layout, self.project_id, destination, self._clock()
        )

    def list_runs(self, run_filter: RunFilter | None = None) -> list[RunSummary]:
        selected = run_filter or RunFilter()
        return self._database.read(lambda conn: select_runs(conn, selected))


def _text(data: JsonObject, key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


def _check_from_payload(action_id: str, data: JsonObject) -> HeuristicCheck:
    actions: list[ActionReference] = []
    raw: JsonValue = data.get("actions")
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        sequence = item.get("sequence")
        digest = item.get("arguments_sha256")
        actions.append(
            ActionReference(
                action_id=_text(item, "action_id"),
                tool_name=_text(item, "tool_name"),
                sequence=sequence if isinstance(sequence, int) else 0,
                arguments_sha256=digest if isinstance(digest, str) else None,
            )
        )
    criterion = data.get("criterion_id")
    return HeuristicCheck(
        action_id=action_id,
        heuristic=_text(data, "heuristic"),
        criterion_id=criterion if isinstance(criterion, str) else None,
        outcome=_text(data, "outcome"),
        actions=tuple(actions),
        note=_text(data, "note"),
    )


def open_history(
    source: ProjectContext | LoadedConfig,
    *,
    clock: Clock = utc_now,
    entropy: Entropy = secrets.token_hex,
    process_checker: ProcessChecker = process_exists,
    retry_policy: RetryPolicy | None = None,
    owner_pid: int | None = None,
) -> HistoryStore:
    """Abre o histórico do projeto, aplica migrations e reconcilia runs órfãs.

    Sem project_id persistido, falha antes de criar diretório ou abrir o banco.
    """
    project_id = source.require_project_id()
    layout = HistoryLayout(source.project_data_dir)
    layout.root.mkdir(parents=True, exist_ok=True)
    database = Database(layout.database, retry_policy)
    apply_migrations(database)
    store = HistoryStore(
        layout,
        database,
        project_id=project_id,
        timezone=source.config.timezone,
        clock=clock,
        entropy=entropy,
        process_checker=process_checker,
        owner_pid=owner_pid,
    )
    store.reconcile_orphans()
    return store
