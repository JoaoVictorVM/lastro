"""Contratos de entrada das tools de achados e critérios, e DTOs do relatório."""

from dataclasses import dataclass
from typing import Annotated, Any, Self, cast

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator

from lastro.history import (
    MAX_EVIDENCE_REFERENCES,
    MAX_FINDING_FLOW_LENGTH,
    MAX_FINDING_PRECONDITIONS,
    MAX_FINDING_STEPS,
    MAX_FINDING_TEXT_LENGTH,
    MAX_FINDING_TITLE_LENGTH,
    CriterionOutcome,
    CriterionResultDraft,
    EvidencePurpose,
    EvidenceReference,
    ExpectedSource,
    FailureDomain,
    FindingClassification,
    FindingDraft,
    ReportingError,
    SeveritySuggestion,
)
from lastro.mission.models import normalize_spaces

MISSING_FINDING_FIELDS = "missing_finding_fields"
INVALID_FINDING = "invalid_finding"
INVALID_RESULT = "invalid_result"
MAX_REASON_LENGTH = 200
MAX_VIDEO_REASON_LENGTH = 500
REQUIRED_FINDING_FIELDS = (
    "title",
    "flow",
    "steps",
    "expected",
    "observed",
    "classification",
    "failure_domain",
)
REQUIRED_EXPECTED_FIELDS = ("text", "source")


def _line(value: str) -> str:
    normalized = normalize_spaces(value)
    if not normalized:
        raise ValueError("text must not be blank")
    return normalized


def _block(value: str) -> str:
    if not value.strip():
        raise ValueError("text must not be blank")
    return value.strip()


type Title = Annotated[str, Field(max_length=MAX_FINDING_TITLE_LENGTH), AfterValidator(_line)]
type Flow = Annotated[str, Field(max_length=MAX_FINDING_FLOW_LENGTH), AfterValidator(_line)]
type Text = Annotated[str, Field(max_length=MAX_FINDING_TEXT_LENGTH), AfterValidator(_block)]
type ArtifactId = Annotated[str, Field(min_length=1, max_length=64)]
# Enums chegam como texto JSON pelo MCP; somente eles aceitam conversão no modelo estrito.
type SourceField = Annotated[ExpectedSource, Field(strict=False)]
type PurposeField = Annotated[EvidencePurpose, Field(strict=False)]
type ClassificationField = Annotated[FindingClassification, Field(strict=False)]
type DomainField = Annotated[FailureDomain, Field(strict=False)]
type SeverityField = Annotated[SeveritySuggestion, Field(strict=False)]
type OutcomeField = Annotated[CriterionOutcome, Field(strict=False)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ExpectedInput(_Strict):
    text: Text
    source: SourceField
    source_ref: Annotated[str, Field(min_length=1, max_length=MAX_FINDING_FLOW_LENGTH)] | None = (
        None
    )


class EvidenceInput(_Strict):
    artifact_id: ArtifactId
    purpose: PurposeField = EvidencePurpose.STATE


class FindingInput(_Strict):
    """Achado enviado pelo agente; ambiente, tenant e versões vêm da run, nunca daqui."""

    title: Title
    flow: Flow
    preconditions: Annotated[list[Text], Field(max_length=MAX_FINDING_PRECONDITIONS)] = Field(
        default_factory=list[str]
    )
    steps: Annotated[list[Text], Field(min_length=1, max_length=MAX_FINDING_STEPS)]
    expected: ExpectedInput
    observed: Text
    evidence: Annotated[list[EvidenceInput], Field(max_length=MAX_EVIDENCE_REFERENCES)] = Field(
        default_factory=list[EvidenceInput]
    )
    classification: ClassificationField
    limitations: Annotated[list[Text], Field(max_length=MAX_FINDING_PRECONDITIONS)] = Field(
        default_factory=list[str]
    )
    failure_domain: DomainField
    severity_suggestion: SeverityField | None = None

    @model_validator(mode="after")
    def _criterion_reference(self) -> Self:
        if self.expected.source == ExpectedSource.MISSION_CRITERION and not (
            self.expected.source_ref
        ):
            raise ValueError("mission_criterion expectations need source_ref with a criterion id")
        return self

    def draft(self, action_id: str | None) -> FindingDraft:
        return FindingDraft(
            title=self.title,
            flow=self.flow,
            preconditions=tuple(self.preconditions),
            steps=tuple(self.steps),
            expected=self.expected.text,
            expected_source=self.expected.source,
            expected_source_ref=self.expected.source_ref,
            observed=self.observed,
            evidence=tuple(
                EvidenceReference(item.artifact_id, item.purpose) for item in self.evidence
            ),
            classification=self.classification,
            limitations=tuple(self.limitations),
            failure_domain=self.failure_domain,
            severity_suggestion=self.severity_suggestion,
            action_id=action_id,
        )


class CriterionResultInput(_Strict):
    criterion_id: Annotated[str, Field(min_length=1, max_length=64)]
    outcome: OutcomeField
    evidence_artifact_ids: Annotated[
        list[ArtifactId], Field(max_length=MAX_EVIDENCE_REFERENCES)
    ] = Field(default_factory=list[str])
    note: str = Field(default="", max_length=MAX_FINDING_TEXT_LENGTH)
    reason: Annotated[str, Field(max_length=MAX_REASON_LENGTH), AfterValidator(_line)]

    def draft(self) -> CriterionResultDraft:
        return CriterionResultDraft(
            criterion_id=self.criterion_id,
            outcome=self.outcome,
            evidence_artifact_ids=tuple(self.evidence_artifact_ids),
            note=self.note.strip(),
            reason=self.reason,
        )


class StartVideoInput(_Strict):
    reason: Annotated[str, Field(max_length=MAX_VIDEO_REASON_LENGTH), AfterValidator(_line)]


class StopVideoInput(_Strict):
    recording_id: Annotated[str, Field(min_length=1, max_length=64)]


def _absent(value: object) -> bool:
    return value is None or (isinstance(value, str | list) and not value)


def _first_error(error: ValidationError) -> str:
    detail = error.errors()[0]
    location = ".".join(str(part) for part in detail["loc"]) or "input"
    return f"{location}: {detail['msg']}"


def parse_finding(raw: dict[str, Any], action_id: str | None) -> FindingDraft:
    """Lista os campos obrigatórios ausentes em ordem antes de validar formato e limites."""
    missing = [name for name in REQUIRED_FINDING_FIELDS if _absent(raw.get(name))]
    expected: object = raw.get("expected")
    if isinstance(expected, dict):
        fields = cast(dict[str, object], expected)
        missing += [
            f"expected.{name}" for name in REQUIRED_EXPECTED_FIELDS if _absent(fields.get(name))
        ]
    if missing:
        raise ReportingError(
            MISSING_FINDING_FIELDS, f"Missing finding fields: {', '.join(sorted(missing))}."
        )
    try:
        return FindingInput.model_validate(raw).draft(action_id)
    except ValidationError as error:
        raise ReportingError(INVALID_FINDING, _first_error(error)) from None


def parse_criterion_result(raw: dict[str, Any]) -> CriterionResultDraft:
    try:
        return CriterionResultInput.model_validate(raw).draft()
    except ValidationError as error:
        raise ReportingError(INVALID_RESULT, _first_error(error)) from None


@dataclass(frozen=True)
class EvidenceView:
    artifact_id: str
    relative_path: str
    artifact_type: str
    sha256: str
    purpose: str
    missing: bool


@dataclass(frozen=True)
class ReproductionView:
    child_run_id: str
    outcome: str
    executor: str
    attempted_at: str


@dataclass(frozen=True)
class FindingView:
    id: str
    title: str
    flow: str
    classification: str
    failure_domain: str
    origin: str
    severity_suggestion: str | None
    environment_url: str
    tenant: str
    system_version: str | None
    preconditions: tuple[str, ...]
    steps: tuple[str, ...]
    expected: str
    expected_source: str
    expected_source_ref: str | None
    observed: str
    evidence: tuple[EvidenceView, ...]
    limitations: tuple[str, ...]
    reproduction_attempts: tuple[ReproductionView, ...]
    fingerprint: str
    created_at: str


@dataclass(frozen=True)
class CriterionView:
    id: str
    text: str
    source: str
    outcome: str
    reason: str
    note: str
    revision: int
    result_source: str
    evidence: tuple[EvidenceView, ...]


@dataclass(frozen=True)
class SignalView:
    id: str
    signal_type: str
    http_method: str | None
    http_status: int | None
    url_path: str
    message: str
    occurrence_count: int
    first_seen_at: str
    last_seen_at: str
    action_id: str | None
    action_sequence: int | None
    action_tool: str | None


@dataclass(frozen=True)
class CreatedRecordView:
    tracking_id: str
    entity: str
    identifier: str
    tenant: str
    cleanup_status: str
    action_id: str | None
    detail_url: str | None
    cleanup_instructions: str | None
    warnings: tuple[str, ...]
    created_at: str


@dataclass(frozen=True)
class BlockView:
    kind: str
    detail: str


@dataclass(frozen=True)
class SummaryView:
    status: str
    status_reason: str | None
    criteria_met: int
    criteria_total: int
    confirmed: int
    suspected: int
    inconclusive: int
    harness_failures: int
    pending_cleanup: int
    integrity_valid: bool

    def lines(self) -> tuple[str, ...]:
        """Resumo de cinco linhas exibido no fim da run."""
        reason = f" ({self.status_reason})" if self.status_reason else ""
        return (
            f"Status: {self.status}{reason}",
            f"Criteria met: {self.criteria_met}/{self.criteria_total}",
            f"Confirmed findings: {self.confirmed}",
            f"Suspected findings: {self.suspected}",
            f"Created records pending cleanup: {self.pending_cleanup}",
        )


@dataclass(frozen=True)
class ModelUsageView:
    """Consumo durável do executor nativo; incerteza nunca é apresentada como zero."""

    calls: int
    uncertain_calls: int
    input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    confirmed_usd: str
    committed_usd: str
    cap_usd: str


@dataclass(frozen=True)
class ChangeContextView:
    """Contexto de mudança da run, só no relatório privado; nada é relido de Git ou GitHub."""

    issue_source: str | None
    issue_sha256: str | None
    issue_comments: str | None
    issue_scan: str | None
    source_state: str
    comparison: str | None
    branch: str | None
    head: str | None
    base: str | None
    merge_base: str | None
    dirty: bool
    remote_freshness: str
    changed: int
    sources: int
    exclusions: tuple[str, ...]
    truncated: bool
    warnings: tuple[str, ...]
    analyzed_version: str
    executed_version: str
    version_match: str
    version_method: str


@dataclass(frozen=True)
class MissionView:
    source: str
    text: str | None
    snapshot_path: str | None
    content_sha256: str | None
    plan_revision: int | None
    objectives: tuple[str, ...]
    scope: tuple[str, ...]
    gaps: tuple[str, ...]
    out_of_scope: tuple[str, ...]
    charter_statement: str | None
    timebox_minutes: int | None
    heuristics: tuple[str, ...]


@dataclass(frozen=True)
class EnvironmentView:
    run_id: str
    profile: str
    environment_url: str
    tenant: str
    executor_name: str
    executor_version: str | None
    lastro_version: str
    skill_bundle_version: str | None
    analyzed_code_version: str | None
    executed_system_version: str | None
    started_at: str
    ended_at: str | None


@dataclass(frozen=True)
class ReportData:
    """Projeção determinística do histórico; o hash destes dados identifica o snapshot."""

    kind: str
    run_id: str
    termination_reason: str | None
    summary: SummaryView
    mission: MissionView
    environment: EnvironmentView
    criteria: tuple[CriterionView, ...]
    confirmed: tuple[FindingView, ...]
    suspected: tuple[FindingView, ...]
    oracle_signals: tuple[SignalView, ...]
    harness_failures: tuple[FindingView, ...]
    blocks: tuple[BlockView, ...]
    created_records: tuple[CreatedRecordView, ...]
    created_records_warning: str | None
    memory_outcome: str
    memory_revision: int
    memory_changed: int
    memory_pending: int
    limitations: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    model_usage: ModelUsageView | None = None
    change_context: ChangeContextView | None = None
