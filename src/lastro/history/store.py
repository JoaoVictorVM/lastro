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
from lastro.history.database import Database, RetryPolicy, db_timestamp
from lastro.history.errors import (
    ActiveRunExistsError,
    DiskFullError,
    HistoryError,
    HistoryUnavailableError,
    InvalidEventError,
    InvalidTransitionError,
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
from lastro.history.models import (
    LIFECYCLE_EVENTS,
    PROCESS_LOST_REASON,
    ArtifactRecord,
    ArtifactType,
    CreatedRun,
    EventRecord,
    EventType,
    ExecutorInfo,
    Mission,
    MissionSource,
    PolicyDecision,
    RunFilter,
    RunPaths,
    RunRecord,
    RunStatus,
    RunSummary,
)
from lastro.history.processes import ProcessChecker, process_exists
from lastro.history.queries import select_runs
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

    def backup(self, destination: Path) -> BackupResult:
        return create_backup(
            self._database, self._layout, self.project_id, destination, self._clock()
        )

    def list_runs(self, run_filter: RunFilter | None = None) -> list[RunSummary]:
        selected = run_filter or RunFilter()
        return self._database.read(lambda conn: select_runs(conn, selected))


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
