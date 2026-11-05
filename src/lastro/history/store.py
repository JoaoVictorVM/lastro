"""Fachada do histórico consumida pela CLI e pelas demais features.

Nenhuma conexão SQLite nem SQL atravessa esta fronteira: entradas e saídas são DTOs tipados.
"""

import contextlib
import dataclasses
import os
import secrets
import shutil
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from lastro import get_version
from lastro.change_context.models import ChangeContextDraft, PreparedContext
from lastro.change_context.versions import version_match
from lastro.config.loader import LoadedConfig, ProjectContext
from lastro.history import approvals, knowledge, native
from lastro.history.artifacts import (
    StoredFile,
    insert_artifact,
    list_artifacts,
    parse_artifact_type,
    resolve_inside,
    store_file,
)
from lastro.history.backup import BackupResult, create_backup
from lastro.history.change_context import (
    CONTEXT_INTEGRITY,
    insert_context,
    last_passed_baseline,
    record_version,
    select_context,
    select_snapshot,
    select_version_observations,
    upsert_snapshot,
)
from lastro.history.created_records import (
    creation_event,
    insert_created_record,
    mark_cleaned,
    select_created_records,
)
from lastro.history.database import Database, RetryPolicy, db_timestamp
from lastro.history.errors import (
    ActiveRunExistsError,
    ApprovalError,
    DiskFullError,
    HistoryError,
    HistoryUnavailableError,
    InvalidEventError,
    InvalidMissionError,
    InvalidTransitionError,
    KnowledgeError,
    LaunchClaimError,
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
from lastro.history.knowledge_recovery import (
    FallbackError,
    export_fallback,
    fallback_path,
    find_fallbacks,
    load_fallback,
    mark_imported,
)
from lastro.history.layout import CONTEXT_DIR, MISSION_SNAPSHOT_RELATIVE, HistoryLayout
from lastro.history.migrations import apply_migrations
from lastro.history.mission_plans import (
    INVALID_PLAN,
    INVALID_REFERENCE,
    answer_clarification,
    blocking_gap,
    make_cursor,
    mission_content,
    paginate,
    parse_cursor,
    request_clarification,
    requirements_text,
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
    ApprovalChoice,
    ApprovalDecision,
    ApprovalRecord,
    ApprovalStatus,
    ArtifactRecord,
    ArtifactType,
    ChangeContextRecord,
    Clarification,
    CleanupResult,
    CleanupStatus,
    CreatedRecord,
    CreatedRecordInput,
    CreatedRun,
    CriterionResult,
    CriterionResultDraft,
    CriterionSubmission,
    DecidedBy,
    EventRecord,
    EventType,
    ExecutorInfo,
    Finding,
    FindingDraft,
    FindingSubmission,
    HeuristicCheck,
    IssueSnapshotRecord,
    JsonObject,
    JsonValue,
    Mission,
    MissionContext,
    MissionPage,
    MissionPlan,
    MissionSource,
    NotificationAttempt,
    NotificationChannel,
    NotificationStatus,
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
    SourceFileRecord,
)
from lastro.history.oracles import record_signal, select_signals
from lastro.history.processes import ProcessChecker, process_exists
from lastro.history.queries import select_runs
from lastro.history.redaction import canonical_json, redact_text, sha256_hex
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
    claim_handoff,
    clear_active_pointer,
    config_snapshot,
    insert_run,
    launch_pending,
    new_run_id,
    publish_active_pointer,
    read_active_pointer,
    select_non_terminal,
    select_run,
    validate_mission,
)
from lastro.knowledge import (
    Category,
    ConsolidationAttempt,
    EditDocument,
    KnowledgeDetail,
    KnowledgeEntry,
    MemoryOutcome,
    MutationOperation,
    MutationResult,
    ObservationRecord,
    ProposalInput,
    ProposalReceipt,
    QueryInput,
    QueryResult,
    RevalidateInput,
    RevalidationResult,
    Scope,
    SourceType,
    ValidationState,
)
from lastro.knowledge.models import Fact
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
LAUNCH_ABANDONED_REASON = "launch_abandoned"
LAUNCH_PUBLISH_FAILED_REASON = "launch_publish_failed"
# Depois de esgotar o retry, a marcação de blocked é uma última tentativa única.
_LAST_ATTEMPT_POLICY = RetryPolicy(delays=())


def _run_exists(conn: sqlite3.Connection, run_id: str) -> bool:
    return conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone() is not None


def _run_snapshot(conn: sqlite3.Connection, run_id: str) -> IssueSnapshotRecord | None:
    snapshot_id = select_run(conn, run_id).issue_snapshot_id
    return select_snapshot(conn, snapshot_id) if snapshot_id is not None else None


def _store_context_files(
    run_dir: Path, change: ChangeContextDraft | None
) -> tuple[StoredFile | None, dict[str, StoredFile], tuple[StoredFile, ...]]:
    """Materializa manifest, resumo, diff e fontes aprovadas na pasta privada antes da transação."""
    if change is None:
        return None, {}, ()
    stored_sources = {
        source.file_id: store_file(
            run_dir, f"{CONTEXT_DIR}/sources/{source.file_id}.txt", content=source.data
        )
        for source in change.sources
    }
    extras = (
        store_file(run_dir, f"{CONTEXT_DIR}/summary.md", content=change.summary.encode("utf-8")),
        store_file(
            run_dir, f"{CONTEXT_DIR}/changes.diff", content=change.diff_text.encode("utf-8")
        ),
    )
    manifest = store_file(
        run_dir,
        f"{CONTEXT_DIR}/manifest.json",
        content=(canonical_json(change.payload()) + "\n").encode("utf-8"),
    )
    return manifest, stored_sources, extras


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
        prepared: PreparedContext | None = None,
        executor_snapshot: JsonObject | None = None,
    ) -> CreatedRun:
        """Cria e confirma a run, a missão e run_started antes de publicar o ponteiro ativo.

        Ao retornar, a run já é durável; só então qualquer interação com o sistema alvo pode
        começar. Snapshot da issue, contexto de mudança, fontes e eventos de segurança entram na
        mesma transação: ou tudo é confirmado, ou nada referencia a run.
        """
        validated = validate_mission(mission)
        issue_capture = prepared.issue if prepared is not None else None
        if issue_capture is not None and validated.source != MissionSource.ISSUE:
            raise InvalidMissionError("issue capture requires an issue mission")
        change = prepared.change if prepared is not None else None
        if analyzed_code_version is None and change is not None:
            analyzed_code_version = change.analyzed_code_version
        active = self.get_active_run()
        if active is not None:
            raise ActiveRunExistsError(active.id)
        self._abandon_pending_launches()
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
                config_snapshot={**config_snapshot(context), **(executor_snapshot or {})},
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

            context_files = _store_context_files(run_dir, change) if prepared is not None else None

            def persist(conn: sqlite3.Connection) -> RunRecord:
                final = record
                if prepared is not None and prepared.issue is not None:
                    snapshot_id = upsert_snapshot(
                        conn,
                        self.project_id,
                        prepared.issue,
                        record.issue_snapshot_id or "",
                        started_at,
                    )
                    final = dataclasses.replace(record, issue_snapshot_id=snapshot_id)
                elif record.issue_snapshot_id is not None:
                    select_snapshot(conn, record.issue_snapshot_id, self.project_id)
                insert_run(conn, final)
                if stored is not None:
                    insert_artifact(conn, run_id, stored, ArtifactType.SNAPSHOT, started_at)
                insert_event(conn, run_id, started, started_at)
                if prepared is not None and context_files is not None:
                    manifest, sources, extras = context_files
                    for item in extras:
                        insert_artifact(conn, run_id, item, ArtifactType.SNAPSHOT, started_at)
                    manifest_id = (
                        insert_artifact(
                            conn, run_id, manifest, ArtifactType.SNAPSHOT, started_at
                        ).id
                        if manifest is not None
                        else None
                    )
                    artifact_ids = {
                        file_id: insert_artifact(
                            conn, run_id, item, ArtifactType.SNAPSHOT, started_at
                        ).id
                        for file_id, item in sources.items()
                    }
                    insert_context(
                        conn,
                        context_id="ctx_" + secrets.token_hex(8),
                        run_id=run_id,
                        issue_snapshot_id=final.issue_snapshot_id,
                        draft=change,
                        issue_capture=prepared.issue,
                        manifest_artifact_id=manifest_id,
                        source_artifacts=artifact_ids,
                        timestamp=started_at,
                    )
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
        if launch_pending(record):
            return None
        if not self._process_checker(record.owner_pid):
            return None
        return record

    def transition_run(
        self, run_id: str, status: RunStatus | str, reason: str | None = None
    ) -> RunRecord:
        target = RunStatus(status)
        timestamp = self._now()
        try:
            record = self._database.write(
                lambda conn: apply_transition(conn, run_id, target, reason, timestamp)
            )
        except HistoryUnavailableError as exc:
            if target.is_terminal:
                self._export_memory_fallback(run_id, str(exc), timestamp)
            raise
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
                if launch_pending(record):
                    continue
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

    def _abandon_pending_launches(self) -> None:
        """Handoff nunca lançado não tocou o alvo; uma nova run o encerra em vez de travar."""
        timestamp = self._now()

        def abandon(conn: sqlite3.Connection) -> list[str]:
            pending = [record.id for record in select_non_terminal(conn) if launch_pending(record)]
            for run_id in pending:
                apply_transition(
                    conn, run_id, RunStatus.INTERRUPTED, LAUNCH_ABANDONED_REASON, timestamp
                )
            return pending

        for run_id in self._database.write(abandon):
            clear_active_pointer(self._layout.active_run, run_id)

    def claim_launch(self, run_id: str) -> RunRecord:
        """Primeira sessão da run: dono e marcador na transação, depois o ponteiro ativo.

        Sem ponteiro publicado o MCP recusaria a run; a falha encerra antes de abrir o filho.
        """
        owner = self.owner_pid
        record = self._database.write(lambda conn: claim_handoff(conn, run_id, owner))
        try:
            publish_active_pointer(self._layout.active_run, record)
        except OSError as error:
            self.finish_run(run_id, RunStatus.INTERRUPTED, LAUNCH_PUBLISH_FAILED_REASON)
            clear_active_pointer(self._layout.active_run, run_id)
            raise LaunchClaimError(f"run {run_id} could not be published: {error}") from None
        return record

    def _export_memory_fallback(self, run_id: str, error: str, timestamp: str) -> None:
        """Propostas confirmadas vão para a pasta privada; a leitura também pode falhar."""
        proposals: list[JsonObject] = []
        with contextlib.suppress(HistoryError, sqlite3.Error):
            proposals = self._database.read(lambda conn: knowledge.pending_snapshot(conn, run_id))
        with contextlib.suppress(OSError):
            export_fallback(
                self._layout.run_dir(run_id),
                project_id=self.project_id,
                run_id=run_id,
                proposals=proposals,
                error=error,
                timestamp=timestamp,
            )

    def recover_memory_fallbacks(self) -> list[str]:
        """Importa fallbacks de runs já terminais antes de iniciar uma nova run."""
        recovered: list[str] = []
        for path in find_fallbacks(self._layout.runs_dir):
            try:
                fallback = load_fallback(path, self.project_id)
                record = self.get_run(fallback.run_id)
            except (FallbackError, OSError, HistoryError):
                continue
            if not record.status.is_terminal:
                continue
            timestamp = self._now()
            self._database.write(
                lambda conn, item=fallback, moment=timestamp: knowledge.retry_consolidation(
                    conn,
                    self.project_id,
                    item.run_id,
                    item.attempt_key,
                    moment,
                    fallback=item.proposals,
                )
            )
            with contextlib.suppress(OSError):
                mark_imported(path)
            recovered.append(fallback.run_id)
        return recovered

    def record_observation(
        self, run_id: str, action_id: str, projection: JsonObject
    ) -> ObservationRecord:
        """Projeção sanitizada do observe; sem commit, nenhum id é devolvido ao agente."""
        timestamp = self._now()
        return self._database.write(
            lambda conn: knowledge.insert_observation(
                conn, self.project_id, run_id, action_id, projection, timestamp
            )
        )

    def get_observation(self, observation_id: str) -> ObservationRecord:
        return self._database.read(lambda conn: knowledge.select_observation(conn, observation_id))

    def propose_knowledge(
        self, run_id: str, proposal: ProposalInput, raw: object = None
    ) -> ProposalReceipt:
        """Proposta e knowledge_proposed juntas; diretiva é recusada e auditada sem eco."""
        if run_id in self._failed_closed:
            raise HistoryUnavailableError()
        timestamp = self._now()
        try:
            return self._database.write(
                lambda conn: knowledge.propose(conn, self.project_id, run_id, proposal, timestamp)
            )
        except KnowledgeError as exc:
            if exc.code == knowledge.POSSIBLE_INJECTION:
                self.record_knowledge_rejection(run_id, exc.code, raw)
            raise

    def record_knowledge_rejection(self, run_id: str, code: str, raw: object) -> None:
        timestamp = self._now()
        self._database.write(
            lambda conn: knowledge.record_rejection(conn, run_id, code, raw, timestamp)
        )

    def query_knowledge(self, run_id: str, request: QueryInput) -> QueryResult:
        now = self._clock()
        timestamp = db_timestamp(now)
        return self._database.write(
            lambda conn: knowledge.query(conn, self.project_id, run_id, request, now, timestamp)
        )

    def revalidate_knowledge(self, run_id: str, request: RevalidateInput) -> RevalidationResult:
        timestamp = self._now()
        return self._database.write(
            lambda conn: knowledge.revalidate(conn, self.project_id, run_id, request, timestamp)
        )

    def list_knowledge(
        self,
        *,
        flow: str | None = None,
        state: ValidationState | None = None,
        include_discarded: bool = False,
    ) -> list[KnowledgeEntry]:
        return self._database.read(
            lambda conn: knowledge.list_entries(
                conn,
                self.project_id,
                flow=flow,
                state=state,
                include_discarded=include_discarded,
            )
        )

    def knowledge_detail(self, entry_id: str) -> KnowledgeDetail:
        return self._database.read(lambda conn: knowledge.detail(conn, self.project_id, entry_id))

    def _mutate(
        self,
        entry_id: str,
        operation: MutationOperation,
        expected_revision: int,
        payload: JsonObject,
        reason: str,
    ) -> MutationResult:
        if not reason.strip():
            raise KnowledgeError(knowledge.INVALID_KNOWLEDGE, "A reason is required.")
        timestamp = self._now()
        return self._database.write(
            lambda conn: knowledge.request_mutation(
                conn,
                self.project_id,
                entry_id,
                operation,
                expected_revision,
                payload,
                redact_text(reason),
                timestamp,
            )
        )

    def edit_knowledge(
        self, entry_id: str, document: EditDocument, expected_revision: int, reason: str
    ) -> MutationResult:
        payload: JsonObject = {"document": document.model_dump(mode="json")}
        return self._mutate(entry_id, MutationOperation.EDIT, expected_revision, payload, reason)

    def discard_knowledge(
        self, entry_id: str, expected_revision: int, reason: str
    ) -> MutationResult:
        return self._mutate(entry_id, MutationOperation.DISCARD, expected_revision, {}, reason)

    def attest_knowledge(
        self,
        entry_id: str,
        expected_revision: int,
        reason: str,
        system_version: str | None = None,
    ) -> MutationResult:
        """Atestado humano: proveniência nova, nunca prova de observação automática."""
        payload: JsonObject = {"system_version": system_version}
        return self._mutate(
            entry_id, MutationOperation.REVALIDATE, expected_revision, payload, reason
        )

    def consolidate_memory(self, run_id: str) -> MemoryOutcome:
        """Retry idempotente da fila e de eventual fallback de uma run terminal."""
        path = fallback_path(self._layout.run_dir(run_id))
        if path.is_file():
            try:
                fallback = load_fallback(path, self.project_id)
            except (FallbackError, OSError) as exc:
                raise KnowledgeError(knowledge.INVALID_REFERENCE, str(exc)) from None
            timestamp = self._now()
            result = self._database.write(
                lambda conn: knowledge.retry_consolidation(
                    conn,
                    self.project_id,
                    run_id,
                    fallback.attempt_key,
                    timestamp,
                    fallback=fallback.proposals,
                )
            )
            with contextlib.suppress(OSError):
                mark_imported(path)
            return result
        timestamp = self._now()
        attempt_key = "retry:" + timestamp
        return self._database.write(
            lambda conn: knowledge.retry_consolidation(
                conn, self.project_id, run_id, attempt_key, timestamp
            )
        )

    def record_adapter_knowledge(
        self,
        *,
        adapter: str,
        source: SourceType,
        source_ref: str,
        flow: str,
        category: Category,
        fact: Fact,
        scope: Scope,
    ) -> KnowledgeEntry:
        """Fronteira para adaptadores confiáveis de documentação, código e issues."""
        timestamp = self._now()

        def write(conn: sqlite3.Connection) -> KnowledgeEntry:
            entry_id = knowledge.adapter_fact(
                conn,
                self.project_id,
                adapter=adapter,
                source=source,
                source_ref=source_ref,
                flow=flow,
                category=category,
                fact=fact,
                scope=scope,
                timestamp=timestamp,
            )
            return knowledge.select_entry(conn, self.project_id, entry_id)

        return self._database.write(write)

    def knowledge_extensions(self, entry_id: str) -> JsonObject:
        return self._database.read(lambda conn: knowledge.entry_extensions(conn, entry_id))

    def memory_outcome(self, run_id: str) -> MemoryOutcome | None:
        return self._database.read(lambda conn: knowledge.select_outcome(conn, run_id))

    def list_consolidation_attempts(self, run_id: str) -> list[ConsolidationAttempt]:
        return self._database.read(lambda conn: knowledge.select_attempts(conn, run_id))

    def fts5_available(self) -> bool:
        return self._database.read(knowledge.fts5_available)

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
        """Missão durável da run; a de arquivo é lida do snapshot privado e conferida.

        A de issue vem do snapshot congelado: acima de 10.000 caracteres, só a primeira página
        é projetada e as demais exigem `mission_page` com o cursor devolvido.
        """
        record = self.get_run(run_id)
        snapshot = (
            self._layout.mission_snapshot(run_id)
            if record.mission_source == MissionSource.FILE
            else None
        )

        def read(conn: sqlite3.Connection) -> tuple[str, ChangeContextRecord | None]:
            return requirements_text(conn, record, snapshot), select_context(conn, run_id)

        text, context = self._database.read(read)
        if record.mission_source == MissionSource.ISSUE:
            digest = sha256_hex(text)
        else:
            text, digest = mission_content(record, snapshot)
        pages = paginate(text)
        content = pages[0]
        next_cursor = make_cursor(run_id, digest, "issue", 1) if len(pages) > 1 else None
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
            pages=len(pages),
            next_cursor=next_cursor,
            change_context=context,
        )

    def mission_page(self, run_id: str, section: str, cursor: str | None) -> MissionPage:
        """Página estável dos requisitos ou do resumo de mudança, só do conteúdo congelado."""
        record = self.get_run(run_id)
        if section == "issue":
            snapshot = (
                self._layout.mission_snapshot(run_id)
                if record.mission_source == MissionSource.FILE
                else None
            )
            text = self._database.read(lambda conn: requirements_text(conn, record, snapshot))
        else:
            text = self._change_summary(run_id)
        digest = sha256_hex(text)
        pages = paginate(text)
        index = 0 if cursor is None else parse_cursor(cursor, run_id, digest, section, len(pages))
        following = index + 1
        return MissionPage(
            section=section,
            index=index,
            total=len(pages),
            content=pages[index],
            next_cursor=make_cursor(run_id, digest, section, following)
            if following < len(pages)
            else None,
        )

    def _change_summary(self, run_id: str) -> str:
        """Resumo de mudança do snapshot privado, conferido pelo hash gravado no manifest."""
        context = self.get_change_context(run_id)
        if context is None:
            return "No change context was collected for this run.\n"
        expected = context.payload.get("summary_sha256")
        if expected is None:
            return "No source was collected for this run.\n"
        path = resolve_inside(self.run_dir(run_id), f"{CONTEXT_DIR}/summary.md")
        try:
            text = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            raise MissionPlanError(CONTEXT_INTEGRITY, "change summary missing") from None
        if sha256_hex(text) != expected:
            raise MissionPlanError(CONTEXT_INTEGRITY, "change summary hash mismatch")
        return text

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
            charter = build_charter(plan.charter, max_minutes, plan.affected_flows)
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

    def request_approval(
        self, run_id: str, action_id: str, fingerprint: str, payload: JsonObject
    ) -> ApprovalRecord:
        """Pedido durável antes de qualquer efeito; a run passa a awaiting_approval."""
        now = self._clock()
        return self._database.write(
            lambda conn: approvals.create_request(
                conn, run_id, action_id, fingerprint, payload, now
            )
        )

    def decide_approval(
        self,
        action_id: str,
        choice: ApprovalChoice | str,
        decided_by: DecidedBy | str,
        *,
        note: str | None = None,
        run_id: str | None = None,
        proof_nonce_sha256: str | None = None,
    ) -> ApprovalRecord:
        """Decisão humana ou do approver roteirizado; nunca executa a ação armazenada."""
        now = self._clock()
        record = self._database.write(
            lambda conn: approvals.decide(
                conn,
                action_id,
                ApprovalChoice(choice),
                DecidedBy(decided_by),
                now,
                note=note,
                run_id=run_id,
                proof_nonce_sha256=proof_nonce_sha256,
            )
        )
        if record.status == ApprovalStatus.ENDED:
            clear_active_pointer(self._layout.active_run, record.run_id)
        return record

    def consume_approval(
        self, run_id: str, action_id: str, fingerprint: str, correlation_id: str
    ) -> ApprovalRecord:
        now = self._clock()
        return self._database.write(
            lambda conn: approvals.consume(
                conn, run_id, action_id, fingerprint, correlation_id, now
            )
        )

    def invalidate_approval(
        self, run_id: str, action_id: str, changed_field: str
    ) -> ApprovalRecord:
        now = self._clock()

        def run(conn: sqlite3.Connection) -> ApprovalRecord:
            record = approvals.select_approval(conn, action_id, run_id)
            if record is None:
                raise ApprovalError(approvals.APPROVAL_NOT_FOUND, f"Unknown approval {action_id}.")
            return approvals.invalidate(conn, record, changed_field, now)

        return self._database.write(run)

    def fail_approval_execution(
        self, run_id: str, action_id: str, error_code: str
    ) -> ApprovalRecord:
        now = self._clock()
        return self._database.write(
            lambda conn: approvals.mark_execution_failed(conn, run_id, action_id, error_code, now)
        )

    def expire_approvals(self, run_id: str | None = None) -> list[ApprovalRecord]:
        """Reconciliação de pedidos vencidos, chamada no startup, em leituras e pelo timer."""
        now = self._clock()
        expired = self._database.write(lambda conn: approvals.expire_due(conn, now, run_id))
        return expired

    def get_approval(self, action_id: str, run_id: str | None = None) -> ApprovalRecord | None:
        return self._database.read(lambda conn: approvals.select_approval(conn, action_id, run_id))

    def pending_approval(self, run_id: str) -> ApprovalRecord | None:
        return self._database.read(lambda conn: approvals.select_pending(conn, run_id))

    def list_approvals(self, run_id: str) -> list[ApprovalRecord]:
        return self._database.read(lambda conn: approvals.select_approvals(conn, run_id))

    def approval_decision(
        self, action_id: str, run_id: str | None = None
    ) -> ApprovalDecision | None:
        """Decisão registrada de um pedido, como o harness de avaliação a consome."""
        record = self.get_approval(action_id, run_id)
        if (
            record is None
            or record.decision is None
            or record.decided_by is None
            or record.decided_at is None
        ):
            return None
        return ApprovalDecision(
            record.action_id, record.decision, record.decided_by, record.decided_at
        )

    def record_notification(
        self,
        record: ApprovalRecord,
        channel: NotificationChannel | str,
        status: NotificationStatus | str,
        *,
        requested_at: datetime,
        latency_ms: int,
        error_code: str | None = None,
        attempt_key: str = "initial",
    ) -> NotificationAttempt:
        completed = self._now()
        return self._database.write(
            lambda conn: approvals.record_notification(
                conn,
                record,
                NotificationChannel(channel),
                NotificationStatus(status),
                db_timestamp(requested_at),
                completed,
                latency_ms,
                error_code,
                attempt_key,
            )
        )

    def list_notifications(self, record: ApprovalRecord) -> list[NotificationAttempt]:
        return self._database.read(lambda conn: approvals.select_notifications(conn, record))

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

    def get_issue_snapshot(self, snapshot_id: str) -> IssueSnapshotRecord:
        return self._database.read(lambda conn: select_snapshot(conn, snapshot_id, self.project_id))

    def get_change_context(self, run_id: str) -> ChangeContextRecord | None:
        """Contexto imutável da run, como o consumidor pós-implantação o lê."""
        return self._database.read(lambda conn: select_context(conn, run_id))

    def read_source_file(self, run_id: str, file_id: str) -> tuple[SourceFileRecord, str]:
        """Conteúdo aprovado e congelado por (run, file_id), conferido pelo SHA-256.

        Só o snapshot privado da run é lido; ids externos, excluídos ou de outra run são negados.
        """
        context = self.get_change_context(run_id)
        source = (
            next((item for item in context.sources if item.id == file_id), None)
            if context is not None
            else None
        )
        if context is None or source is None:
            raise MissionPlanError(INVALID_REFERENCE, "file_id is not in this run's source set")
        artifact = next(
            (item for item in self.list_artifacts(run_id) if item.id == source.artifact_id), None
        )
        if artifact is None:
            raise MissionPlanError(CONTEXT_INTEGRITY, "source artifact missing")
        try:
            data = resolve_inside(self.run_dir(run_id), artifact.relative_path).read_bytes()
        except OSError:
            raise MissionPlanError(CONTEXT_INTEGRITY, "source artifact missing") from None
        if sha256_hex(data) != source.content_sha256 or sha256_hex(data) != artifact.sha256:
            raise MissionPlanError(CONTEXT_INTEGRITY, "source artifact hash mismatch")
        return source, data.decode("utf-8")

    def observe_executed_version(
        self, run_id: str, executed: str | None, *, method: str, origin: str
    ) -> JsonObject:
        """Versão realmente obtida do alvo; a correspondência nunca vem de nome de branch."""
        record = self.get_run(run_id)
        if record.status.is_terminal:
            raise RunNotActiveError(run_id, record.status.value)
        context = self.get_change_context(run_id)
        dirty = bool(context.payload.get("dirty")) if context is not None else False
        observation: JsonObject = {
            "analyzed_code_version": record.analyzed_code_version,
            "executed_system_version": executed,
            "version_match": version_match(record.analyzed_code_version, executed, dirty).value,
            "method": method,
            "origin": origin,
            "observed_at": self._now(),
        }
        timestamp = self._now()
        self._database.write(lambda conn: record_version(conn, run_id, observation, timestamp))
        return observation

    def last_passed_baseline(
        self, profile: str, tenant: str, flow_key: str
    ) -> tuple[str, str] | None:
        return self._database.read(
            lambda conn: last_passed_baseline(conn, self.project_id, profile, tenant, flow_key)
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
                memory=knowledge.select_outcome(conn, run_id),
                model_usage=native.usage(conn, run_id)
                if native.has_context(conn, run_id)
                else None,
                change_context=select_context(conn, run_id),
                issue_snapshot=_run_snapshot(conn, run_id),
                version_observations=tuple(select_version_observations(conn, run_id)),
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

    def create_suite_budget(self, cap_nusd: int) -> str:
        """Orçamento de suíte do projeto; o id vem daqui, nunca do modelo nem do runner."""
        timestamp = self._now()
        return self._database.write(
            lambda conn: native.create_suite_budget(conn, self.project_id, cap_nusd, timestamp)
        )

    def start_native_run(self, data: native.NativeContextInput) -> None:
        """Orçamento da run, snapshot nativo e native_started numa única transação."""
        timestamp = self._now()
        self._database.write(
            lambda conn: native.create_run_context(conn, self.project_id, data, timestamp)
        )

    def has_native_context(self, run_id: str) -> bool:
        return self._database.read(lambda conn: native.has_context(conn, run_id))

    def reserve_model_call(self, data: native.ReservationInput) -> native.ModelCall:
        if data.run_id in self._failed_closed:
            raise HistoryUnavailableError()
        timestamp = self._now()
        call_id = "call_" + secrets.token_hex(12)
        return self._database.write(lambda conn: native.reserve(conn, data, call_id, timestamp))

    def mark_model_call_sent(self, call_id: str) -> native.ModelCall:
        timestamp = self._now()
        return self._database.write(lambda conn: native.mark_sent(conn, call_id, timestamp))

    def settle_model_call(self, call_id: str, data: native.Settlement) -> native.ModelCall:
        timestamp = self._now()
        return self._database.write(lambda conn: native.settle(conn, call_id, data, timestamp))

    def fail_model_call(
        self,
        call_id: str,
        *,
        released: bool,
        error_code: str,
        latency_ms: int | None = None,
        request_id: str | None = None,
    ) -> native.ModelCall:
        timestamp = self._now()
        return self._database.write(
            lambda conn: native.fail(
                conn,
                call_id,
                released=released,
                error_code=error_code,
                latency_ms=latency_ms,
                request_id=request_id,
                timestamp=timestamp,
            )
        )

    def list_model_calls(self, run_id: str) -> list[native.ModelCall]:
        return self._database.read(lambda conn: native.select_calls(conn, run_id))

    def native_usage(self, run_id: str) -> native.NativeUsage | None:
        def read(conn: sqlite3.Connection) -> native.NativeUsage | None:
            return native.usage(conn, run_id) if native.has_context(conn, run_id) else None

        return self._database.read(read)

    def reconcile_model_calls(self) -> list[str]:
        timestamp = self._now()
        return self._database.write(lambda conn: native.reconcile_terminal(conn, timestamp))

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
    store.reconcile_model_calls()
    store.recover_memory_fallbacks()
    store.expire_approvals()
    return store
