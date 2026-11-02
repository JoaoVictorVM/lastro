"""Contratos de entrada das tools de achados e critérios, e DTOs do relatório."""

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
