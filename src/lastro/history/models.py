"""Contratos tipados do histórico: estados, eventos, missões, registros e filtros."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urljoin

if TYPE_CHECKING:
    from lastro.history.native import NativeUsage

from lastro.knowledge.models import MemoryOutcome
from lastro.oracles.models import OracleSignal

MAX_MISSION_TEXT_LENGTH = 10_000
MAX_PREVIEW_LENGTH = 200
DEFAULT_HISTORY_LIMIT = 20
PROCESS_LOST_REASON = "process_lost"

type JsonValue = dict[str, JsonValue] | list[JsonValue] | str | int | float | bool | None
type JsonObject = dict[str, JsonValue]


class RunStatus(StrEnum):
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    LIMIT_REACHED = "limit_reached"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STATUSES

    @property
    def is_waiting(self) -> bool:
        return self in WAITING_STATUSES


TERMINAL_STATUSES = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.BLOCKED,
        RunStatus.INTERRUPTED,
        RunStatus.CANCELLED,
        RunStatus.LIMIT_REACHED,
    }
)
WAITING_STATUSES = frozenset({RunStatus.AWAITING_APPROVAL, RunStatus.AWAITING_CLARIFICATION})
NON_TERMINAL_STATUSES = frozenset(RunStatus) - TERMINAL_STATUSES


def can_transition(current: RunStatus, target: RunStatus) -> bool:
    """Terminais são imutáveis; espera volta a running ou termina; running espera ou termina."""
    if current.is_terminal or current == target:
        return False
    if current.is_waiting:
        return target == RunStatus.RUNNING or target.is_terminal
    return True


class EventType(StrEnum):
    RUN_STARTED = "run_started"
    TOOL_CALL = "tool_call"
    POLICY_DECISION = "policy_decision"
    AUTHENTICATION = "authentication"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_DECIDED = "approval_decided"
    CLARIFICATION_REQUESTED = "clarification_requested"
    CLARIFICATION_ANSWERED = "clarification_answered"
    ORACLE_SIGNAL = "oracle_signal"
    FINDING_SUBMITTED = "finding_submitted"
    RECORD_CREATED = "record_created"
    KNOWLEDGE_PROPOSED = "knowledge_proposed"
    LIMIT_HIT = "limit_hit"
    RUN_ENDED = "run_ended"
    NOTIFICATION_DEGRADED = "notification_degraded"
    NATIVE_STARTED = "native_started"
    MODEL_CALL_STARTED = "model_call_started"
    MODEL_CALL_COMPLETED = "model_call_completed"
    MODEL_CALL_FAILED = "model_call_failed"
    AGENT_FAILURE = "agent_failure"
    CHANGE_CONTEXT_COLLECTED = "change_context_collected"
    SECURITY_EVENT = "security_event"
    VERSION_OBSERVED = "version_observed"


# Somente o registro abre e fecha a run; consumidores não anexam esses dois tipos.
LIFECYCLE_EVENTS = frozenset({EventType.RUN_STARTED, EventType.RUN_ENDED})
CORRELATED_EVENTS = frozenset({EventType.TOOL_CALL, EventType.POLICY_DECISION})


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class CleanupStatus(StrEnum):
    PENDING = "pending"
    CLEANED_MANUALLY = "cleaned_manually"
    NOT_REQUIRED = "not_required"


UNKNOWN_IDENTIFIER = "unknown"


class MissionSource(StrEnum):
    TEXT = "text"
    FILE = "file"
    ISSUE = "issue"


class ArtifactType(StrEnum):
    SCREENSHOT = "screenshot"
    VIDEO = "video"
    REPORT = "report"
    SNAPSHOT = "snapshot"


@dataclass(frozen=True)
class TextMission:
    text: str


@dataclass(frozen=True)
class FileMission:
    path: Path


@dataclass(frozen=True)
class IssueMission:
    issue_snapshot_id: str
    text: str | None = None


type Mission = TextMission | FileMission | IssueMission


@dataclass(frozen=True)
class IssueSnapshotRecord:
    """Snapshot imutável de uma issue importada, já sanitizado."""

    id: str
    project_id: str
    source_kind: str
    repository: str | None
    issue_number: int | None
    source_identity: str
    content_sha256: str
    payload: JsonObject
    captured_at: str


@dataclass(frozen=True)
class SourceFileRecord:
    id: str
    context_id: str
    relative_path: str
    origin: str
    revision_oid: str | None
    content_sha256: str
    size_bytes: int
    artifact_id: str


@dataclass(frozen=True)
class ChangeContextRecord:
    """Captura congelada da run: manifest sanitizado, fontes aprovadas e captura própria."""

    id: str
    run_id: str
    issue_snapshot_id: str | None
    source_state: str
    flow_key: str | None
    captured_at: str
    content_sha256: str
    payload: JsonObject
    manifest_artifact_id: str | None
    sources: tuple[SourceFileRecord, ...] = ()


@dataclass(frozen=True)
class ExecutorInfo:
    name: str
    version: str | None = None


@dataclass(frozen=True)
class RunRecord:
    id: str
    project_id: str
    profile: str
    environment_url: str
    tenant: str
    mission_source: MissionSource
    mission_text: str | None
    mission_snapshot_path: str | None
    mission_file_sha256: str | None
    issue_snapshot_id: str | None
    started_at: str
    ended_at: str | None
    status: RunStatus
    status_reason: str | None
    owner_pid: int
    executor_name: str
    executor_version: str | None
    lastro_version: str
    skill_bundle_version: str | None
    config_snapshot: JsonObject
    analyzed_code_version: str | None
    executed_system_version: str | None


@dataclass(frozen=True)
class RunPaths:
    run_dir: Path
    evidence_dir: Path
    mission_snapshot: Path | None


@dataclass(frozen=True)
class CreatedRun:
    record: RunRecord
    paths: RunPaths


@dataclass(frozen=True)
class EventRecord:
    id: int
    run_id: str
    sequence: int
    timestamp: str
    event_type: EventType
    tool_name: str | None
    arguments_sha256: str | None
    arguments_preview: str | None
    policy_decision: PolicyDecision | None
    correlation_id: str | None
    idempotency_key: str | None
    payload: JsonValue
    result: JsonValue


@dataclass(frozen=True)
class ArtifactRecord:
    id: str
    run_id: str
    relative_path: str
    artifact_type: ArtifactType
    size_bytes: int
    sha256: str
    created_at: str


@dataclass(frozen=True)
class RunFilter:
    """Filtros do histórico, combinados por AND."""

    flow: str | None = None
    status: RunStatus | None = None
    issue: str | None = None
    profile: str | None = None
    since: datetime | None = None
    limit: int = DEFAULT_HISTORY_LIMIT


@dataclass(frozen=True)
class FindingCounts:
    confirmed: int = 0
    suspected: int = 0
    inconclusive: int = 0


@dataclass(frozen=True)
class RunSummary:
    record: RunRecord
    flows: tuple[str, ...] = ()
    findings: FindingCounts = field(default_factory=FindingCounts)
    pending_cleanup: int = 0


@dataclass(frozen=True)
class CreatedRecordInput:
    """Criação confirmada pela resposta real do alvo; nunca informada pelo modelo."""

    run_id: str
    action_id: str
    entity: str
    system_identifier: str | None
    tenant: str
    expected_tenant: str
    cleanup_required: bool = True
    detail_path: str | None = None
    cleanup_instructions: str | None = None
    unexpected_success_status: int | None = None


@dataclass(frozen=True)
class CreatedRecord:
    id: str
    run_id: str
    profile: str
    environment_url: str
    entity_type: str
    system_identifier: str
    tenant: str
    cleanup_status: CleanupStatus
    created_at: str
    cleaned_at: str | None
    action_id: str | None
    identified: bool
    expected_tenant: str | None
    detail_path: str | None
    cleanup_instructions: str | None
    warnings: tuple[str, ...]
    unexpected_success_status: int | None

    @property
    def detail_url(self) -> str | None:
        if self.detail_path is None:
            return None
        return urljoin(self.environment_url, self.detail_path)


@dataclass(frozen=True)
class CleanupResult:
    record: CreatedRecord
    changed: bool


def unidentified_warning(run_id: str, records: list[CreatedRecord]) -> str | None:
    """Alerta agregado dos registros sem id, no texto em inglês definido para o relatório."""
    count = sum(1 for record in records if not record.identified)
    if count == 0:
        return None
    noun = "record" if count == 1 else "records"
    return (
        f"{count} created {noun} could not be identified automatically; "
        f"check the target system for names starting with LASTRO-{run_id[-4:]}."
    )


class PlanStatus(StrEnum):
    MISSING = "missing"
    ACTIVE = "active"
    BLOCKED = "blocked"


class ClarificationStatus(StrEnum):
    PENDING = "pending"
    ANSWERED = "answered"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class MissionContext:
    """Fonte durável da missão: dados de requisito, nunca configuração de tools ou políticas."""

    run_id: str
    source: MissionSource
    content: str
    content_sha256: str
    skill_bundle_sha256: str | None
    plan_status: PlanStatus
    # Páginas de requisitos; acima de uma, a projeção inicial é parcial e o restante exige cursor.
    pages: int = 1
    next_cursor: str | None = None
    change_context: "ChangeContextRecord | None" = None


@dataclass(frozen=True)
class MissionPage:
    """Página estável do conteúdo congelado da run, entregue como dado não confiável."""

    section: str
    index: int
    total: int
    content: str
    next_cursor: str | None


@dataclass(frozen=True)
class PlanCriterion:
    id: str
    text: str
    source: str
    source_ref: str | None = None


@dataclass(frozen=True)
class PlanGap:
    id: str
    description: str
    blocking: bool


@dataclass(frozen=True)
class MissionPlan:
    id: str
    run_id: str
    revision: int
    content_sha256: str
    objectives: tuple[str, ...]
    scope: tuple[str, ...]
    criteria: tuple[PlanCriterion, ...]
    gaps: tuple[PlanGap, ...]
    out_of_scope: tuple[str, ...]
    charter: JsonObject
    revision_reason: str
    clarification_id: str | None
    created_at: str
    # Aditivos e vazios em planos antigos: informação, hipóteses e fluxos não são critérios.
    supporting_information: tuple[JsonObject, ...] = ()
    hypotheses: tuple[JsonObject, ...] = ()
    affected_flows: tuple[JsonObject, ...] = ()

    @property
    def blocking_gaps(self) -> tuple[PlanGap, ...]:
        return tuple(gap for gap in self.gaps if gap.blocking)

    @property
    def exploration_allowed(self) -> bool:
        return not self.blocking_gaps


@dataclass(frozen=True)
class PlanSubmission:
    plan: MissionPlan
    created: bool


@dataclass(frozen=True)
class Clarification:
    id: str
    run_id: str
    mission_plan_id: str
    gap_id: str
    question: str
    why_it_matters: str
    options: tuple[str, ...]
    status: ClarificationStatus
    answer: JsonObject | None
    requested_at: str
    answered_at: str | None


@dataclass(frozen=True)
class ActionReference:
    action_id: str
    tool_name: str
    sequence: int
    arguments_sha256: str | None


@dataclass(frozen=True)
class HeuristicCheck:
    """Aplicação de heurística confirmada por ações reais da mesma run."""

    action_id: str
    heuristic: str
    criterion_id: str | None
    outcome: str
    actions: tuple[ActionReference, ...]
    note: str


MAX_FINDING_TITLE_LENGTH = 120
MAX_FINDING_FLOW_LENGTH = 200
MAX_FINDING_TEXT_LENGTH = 4000
MAX_FINDING_PRECONDITIONS = 20
MAX_FINDING_STEPS = 30
MAX_EVIDENCE_REFERENCES = 20
NOT_EVALUATED_REASON = "not_evaluated"


class FindingClassification(StrEnum):
    SUSPECTED = "suspected"
    CONFIRMED = "confirmed"
    INCONCLUSIVE = "inconclusive"


class FailureDomain(StrEnum):
    SYSTEM = "system"
    AGENT = "agent"
    TOOL = "tool"


class FindingOrigin(StrEnum):
    AGENT = "agent"
    ORACLE = "oracle"


class ExpectedSource(StrEnum):
    MISSION_CRITERION = "mission_criterion"
    DOCUMENTATION = "documentation"
    USER_ANSWER = "user_answer"
    HEURISTIC = "heuristic"
    COMMON_CONVENTION = "common_convention"


class EvidencePurpose(StrEnum):
    STATE = "state"
    BEFORE = "before"
    AFTER = "after"
    ERROR = "error"
    TIMING = "timing"


class SeveritySuggestion(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ReproductionOutcome(StrEnum):
    REPRODUCED = "reproduced"
    NOT_REPRODUCED = "not_reproduced"
    INCONCLUSIVE = "inconclusive"


class CriterionOutcome(StrEnum):
    MET = "met"
    NOT_MET = "not_met"
    BLOCKED = "blocked"
    INCONCLUSIVE = "inconclusive"


class ResultSource(StrEnum):
    AGENT = "agent"
    SYSTEM = "system"


class ReportKind(StrEnum):
    FINAL = "final"
    PARTIAL = "partial"
    PUBLIC = "public"


class SnapshotStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    FALLBACK = "fallback"
    FAILED = "failed"


@dataclass(frozen=True)
class EvidenceReference:
    artifact_id: str
    purpose: EvidencePurpose


@dataclass(frozen=True)
class FindingDraft:
    """Achado já validado pelo contrato da tool; proveniência vem sempre da run."""

    title: str
    flow: str
    preconditions: tuple[str, ...]
    steps: tuple[str, ...]
    expected: str
    expected_source: ExpectedSource
    expected_source_ref: str | None
    observed: str
    evidence: tuple[EvidenceReference, ...]
    classification: FindingClassification
    limitations: tuple[str, ...]
    failure_domain: FailureDomain
    severity_suggestion: SeveritySuggestion | None = None
    origin: FindingOrigin = FindingOrigin.AGENT
    action_id: str | None = None


@dataclass(frozen=True)
class FindingEvidence:
    artifact_id: str
    purpose: EvidencePurpose
    relative_path: str
    artifact_type: ArtifactType
    sha256: str


@dataclass(frozen=True)
class ReproductionAttempt:
    id: str
    finding_id: str
    child_run_id: str
    outcome: ReproductionOutcome
    evidence_artifact_ids: tuple[str, ...]
    executor: str
    attempted_at: str


@dataclass(frozen=True)
class Finding:
    id: str
    run_id: str
    title: str
    flow: str
    preconditions: tuple[str, ...]
    steps: tuple[str, ...]
    expected: str
    expected_source: ExpectedSource
    expected_source_ref: str | None
    observed: str
    evidence: tuple[FindingEvidence, ...]
    classification: FindingClassification
    limitations: tuple[str, ...]
    origin: FindingOrigin
    failure_domain: FailureDomain
    severity_suggestion: SeveritySuggestion | None
    environment_url: str
    tenant: str
    system_version: str | None
    fingerprint: str
    action_id: str | None
    version: int
    reproduction_attempts: tuple[ReproductionAttempt, ...]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class FindingSubmission:
    finding: Finding
    created: bool


@dataclass(frozen=True)
class CriterionResultDraft:
    criterion_id: str
    outcome: CriterionOutcome
    evidence_artifact_ids: tuple[str, ...]
    note: str
    reason: str
    source: ResultSource = ResultSource.AGENT


@dataclass(frozen=True)
class CriterionResult:
    id: str
    run_id: str
    criterion_id: str
    revision: int
    outcome: CriterionOutcome
    evidence_artifact_ids: tuple[str, ...]
    note: str
    source: ResultSource
    reason: str
    content_sha256: str
    created_at: str


@dataclass(frozen=True)
class CriterionSubmission:
    result: CriterionResult
    created: bool


@dataclass(frozen=True)
class ReportSnapshot:
    id: str
    run_id: str
    kind: ReportKind
    input_sha256: str
    status: SnapshotStatus
    report_artifact_id: str | None
    fallback_artifact_id: str | None
    issue_artifact_ids: tuple[str, ...]
    integrity_valid: bool | None
    error_text: str | None
    termination_reason: str | None
    duration_ms: int | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ReportInputs:
    """Leitura consistente de tudo que o relatório projeta, numa única transação."""

    run: RunRecord
    events: tuple[EventRecord, ...]
    artifacts: tuple[ArtifactRecord, ...]
    findings: tuple[Finding, ...]
    criterion_results: tuple[CriterionResult, ...]
    plan: MissionPlan | None
    clarifications: tuple[Clarification, ...]
    created_records: tuple[CreatedRecord, ...]
    oracle_signals: tuple[OracleSignal, ...]
    # Resultado corrente da memória; None em runs anteriores à memória persistente.
    memory: MemoryOutcome | None = None
    # Consumo de modelo do executor nativo; None para executores externos.
    model_usage: "NativeUsage | None" = None
    # Contexto de mudança e issue importada; None nas runs sem esses vínculos.
    change_context: ChangeContextRecord | None = None
    issue_snapshot: IssueSnapshotRecord | None = None
    version_observations: tuple[JsonObject, ...] = ()


APPROVAL_TTL_SECONDS = 900
MAX_APPROVAL_NOTE_LENGTH = 2000
NO_RESPONSE_REASON = "no_response"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    SKIPPED = "skipped"
    ENDED = "ended"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"
    CONSUMED = "consumed"
    EXECUTION_FAILED = "execution_failed"


class ApprovalChoice(StrEnum):
    APPROVE = "approve"
    SKIP = "skip"
    END_RUN = "end_run"


class DecidedBy(StrEnum):
    CLI = "cli"
    EXECUTOR_PROMPT = "executor_prompt"
    SYSTEM = "system"
    # Reservado ao approver automatizado da avaliação; passa pela mesma fachada e pelo mesmo
    # fingerprint que as decisões humanas.
    SCRIPTED = "scripted"


class NotificationChannel(StrEnum):
    EXECUTOR_HOOK = "executor_hook"
    DESKTOP = "desktop"
    TAIL = "tail"


class NotificationStatus(StrEnum):
    DELIVERED = "delivered"
    DEGRADED = "degraded"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class ApprovalRecord:
    """Pedido de aprovação; `action_id` é o id público usado em `lastro approve`."""

    id: str
    run_id: str
    action_id: str
    fingerprint: str
    status: ApprovalStatus
    decision: ApprovalChoice | None
    decided_by: DecidedBy | None
    decision_reason: str | None
    payload: JsonObject
    requested_at: str
    expires_at: str
    decided_at: str | None
    invalidated_at: str | None
    consumed_at: str | None
    execution_correlation_id: str | None

    @property
    def summary(self) -> str:
        value = self.payload.get("summary")
        return value if isinstance(value, str) else self.action_id


@dataclass(frozen=True)
class ApprovalDecision:
    """Interface de decisão consumida pelo harness de avaliação."""

    action_id: str
    decision: ApprovalChoice
    decided_by: DecidedBy
    timestamp: str


@dataclass(frozen=True)
class NotificationAttempt:
    approval_id: str
    channel: NotificationChannel
    status: NotificationStatus
    requested_at: str
    completed_at: str | None
    latency_ms: int | None
    error_code: str | None
