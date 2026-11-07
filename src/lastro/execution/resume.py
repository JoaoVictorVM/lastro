"""Retomada segura: preflight da origem, nova run vinculada e classificação de escritas.

A classificação compara somente valores de negócio mapeados pelo binding, lidos pelo servidor
no estado renderizado do registro. Nada vindo do agente prova que uma escrita foi aplicada.
"""

from dataclasses import dataclass

from lastro.config.loader import ProjectContext
from lastro.execution.models import ReconciliationState, WriteIntent
from lastro.history import (
    CreatedRun,
    ExecutorInfo,
    FileMission,
    HistoryError,
    HistoryStore,
    IssueMission,
    JsonObject,
    Mission,
    MissionSource,
    RunNotFoundError,
    RunRecord,
    RunStatus,
    TextMission,
)

RESUMABLE_STATUSES = frozenset(
    {RunStatus.INTERRUPTED, RunStatus.CANCELLED, RunStatus.LIMIT_REACHED}
)
RESUME_SOURCE_ACTIVE = "resume_source_active"
RESUME_NOT_ELIGIBLE = "resume_not_eligible"
RESUME_CONTEXT_MISMATCH = "resume_context_mismatch"
RESUME_RECORD_MISSING = "resume_record_missing"
RESUME_STATE_CONFLICT = "resume_state_conflict"
RESUME_UNVERIFIABLE = "resume_unverifiable"


class ResumeError(HistoryError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def missing_record_message(record_id: str | None) -> str:
    return f"Record {record_id or 'unknown'} not found on resume; no write repeated."


def preflight(store: HistoryStore, context: ProjectContext, run_id: str) -> RunRecord:
    """Valida origem, projeto, perfil, ambiente e tenant antes de criar run ou abrir browser."""
    source = store.get_run(run_id)
    if source.project_id != store.project_id:
        raise RunNotFoundError(run_id)
    if not source.status.is_terminal:
        raise ResumeError(
            RESUME_SOURCE_ACTIVE,
            f"Run {run_id} is still active; cancel it first with 'lastro run cancel {run_id}'.",
        )
    if source.status not in RESUMABLE_STATUSES:
        raise ResumeError(
            RESUME_NOT_ELIGIBLE,
            f"Run {run_id} ended as {source.status.value}; start a new mission explicitly.",
        )
    profile = context.profile
    if (
        source.profile != context.profile_name
        or source.tenant != profile.tenant_id
        or source.environment_url != profile.target_url
    ):
        raise ResumeError(
            RESUME_CONTEXT_MISMATCH,
            f"Run {run_id} belongs to profile '{source.profile}' (tenant '{source.tenant}'); "
            "select the same profile, environment and tenant to resume it.",
        )
    return source


def mission_of(store: HistoryStore, source: RunRecord) -> Mission:
    """Missão canônica congelada da origem; nunca relê arquivo mutável nem busca a issue."""
    match source.mission_source:
        case MissionSource.TEXT:
            return TextMission(source.mission_text or "")
        case MissionSource.FILE:
            snapshot = store.run_paths(source.id).mission_snapshot
            if snapshot is None:
                raise ResumeError(RESUME_NOT_ELIGIBLE, "The source mission snapshot is missing.")
            return FileMission(snapshot)
        case MissionSource.ISSUE:
            if source.issue_snapshot_id is None:
                raise ResumeError(RESUME_NOT_ELIGIBLE, "The source issue snapshot is missing.")
            return IssueMission(
                issue_snapshot_id=source.issue_snapshot_id, text=source.mission_text
            )


def create_resumed_run(
    store: HistoryStore,
    context: ProjectContext,
    source: RunRecord,
    *,
    executor: ExecutorInfo | None = None,
    skill_bundle_version: str | None = None,
    executor_snapshot: JsonObject | None = None,
) -> CreatedRun:
    """Nova run com orçamento novo e snapshot atual; o vínculo nasce na mesma transação."""
    created = store.create_run(
        context,
        mission_of(store, source),
        executor,
        skill_bundle_version=skill_bundle_version,
        executor_snapshot=executor_snapshot,
        parent_run_id=source.id,
    )
    mark_ambiguous_creations(store, created.record.id)
    return created


def mark_ambiguous_creations(store: HistoryStore, run_id: str) -> None:
    """Criação sem resposta e sem id confiável fica para conferência manual, nunca repetida."""
    for intent in store.unresolved_writes(run_id):
        if intent.operation == "create" and intent.record_id is None:
            store.reconcile_write(
                run_id,
                intent.id,
                ReconciliationState.INCONCLUSIVE,
                f"Create of {intent.entity} without a reliable id; verify manually. "
                "No write repeated.",
            )


@dataclass(frozen=True)
class Classification:
    state: ReconciliationState
    code: str | None
    reason: str


def classify(intent: WriteIntent, current: dict[str, str | None] | None) -> Classification:
    """Compara o estado observado com a intenção, só nos campos de negócio comparáveis."""
    if current is None:
        return Classification(
            ReconciliationState.INCONCLUSIVE,
            RESUME_RECORD_MISSING,
            missing_record_message(intent.record_id),
        )
    if not intent.comparable or not intent.intended:
        return Classification(
            ReconciliationState.INCONCLUSIVE,
            RESUME_UNVERIFIABLE,
            "Intended values are not verifiable (redacted or not observed); no write repeated.",
        )
    keys = sorted(intent.intended)
    observed = {key: current.get(key) for key in keys}
    if any(value is None for value in observed.values()):
        return Classification(
            ReconciliationState.INCONCLUSIVE,
            RESUME_UNVERIFIABLE,
            "The current record could not be read for every mapped field; no write repeated.",
        )
    if all(observed[key] == intent.intended[key] for key in keys):
        return Classification(
            ReconciliationState.ALREADY_APPLIED,
            None,
            "Current values already match the intended edit; it was not sent again.",
        )
    before = intent.before or {}
    if before and all(key in before and observed[key] == before[key] for key in keys):
        return Classification(
            ReconciliationState.READY_FOR_REVIEW,
            None,
            "Current values match the state before the interrupted edit; a new send needs the "
            "current policy and approval.",
        )
    return Classification(
        ReconciliationState.CONFLICT,
        RESUME_STATE_CONFLICT,
        "Current values differ from both the previous and the intended state; not overwritten.",
    )
