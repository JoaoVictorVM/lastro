"""Contratos da missão estruturada submetida pelo agente e validada pelo Python."""

import re
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

MAX_ITEM_LENGTH = 1000
MAX_OBJECTIVES = 10
MAX_SCOPE = 30
MAX_CRITERIA = 50
MAX_GAPS = 20
MAX_OUT_OF_SCOPE = 30
MAX_RESOURCES = 10
MAX_NOTE_LENGTH = 2000
MAX_ACTION_IDS = 20
MAX_QUESTION_LENGTH = 1000
MAX_OPTION_LENGTH = 500
PLAN_SCHEMA_VERSION = 1

_SPACES = re.compile(r"\s+")


class Heuristic(StrEnum):
    BOUNDARY_VALUES = "boundary_values"
    EQUIVALENCE_PARTITIONING = "equivalence_partitioning"
    CRUD_CONSISTENCY = "crud_consistency"
    PERSISTENCE_AFTER_RELOAD = "persistence_after_reload"
    FIELD_VALIDATION = "field_validation"
    PERMISSION_TENANT_ISOLATION = "permission_tenant_isolation"
    ERROR_MESSAGE_QUALITY = "error_message_quality"
    STATE_TRANSITIONS = "state_transitions"


class CriterionSource(StrEnum):
    MISSION_EXPLICIT = "mission_explicit"
    CLARIFICATION = "clarification"


class RevisionReason(StrEnum):
    INITIAL = "initial"
    CLARIFICATION = "clarification"


class CheckOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


def normalize_spaces(value: str) -> str:
    """Colapsa espaços sem mudar caixa nem pontuação exibida."""
    return _SPACES.sub(" ", value).strip()


def _item(value: str) -> str:
    normalized = normalize_spaces(value)
    if not normalized:
        raise ValueError("item must not be blank")
    return normalized


type Item = Annotated[str, Field(max_length=MAX_ITEM_LENGTH), AfterValidator(_item)]


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    return [value for value in values if not (value in seen or seen.add(value))]


type ItemList = Annotated[list[Item], AfterValidator(_dedupe)]


# Enums chegam como texto JSON pelo MCP; somente eles aceitam conversão no modelo estrito.
type HeuristicField = Annotated[Heuristic, Field(strict=False)]
type SourceField = Annotated[CriterionSource, Field(strict=False)]
type ReasonField = Annotated[RevisionReason, Field(strict=False)]
type OutcomeField = Annotated[CheckOutcome, Field(strict=False)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Criterion(_Strict):
    text: Item
    source: SourceField


class Gap(_Strict):
    description: Item
    blocking: bool


class CharterInput(_Strict):
    target: Item
    resources: Annotated[ItemList, Field(min_length=1, max_length=MAX_RESOURCES)]
    information: Item
    timebox_minutes: int = Field(default=20, ge=1)
    heuristics: Annotated[list[HeuristicField], Field(min_length=1, max_length=len(Heuristic))]

    @model_validator(mode="after")
    def _unique_heuristics(self) -> Self:
        if len(set(self.heuristics)) != len(self.heuristics):
            raise ValueError("heuristics must not repeat")
        return self


class MissionPlanInput(_Strict):
    """Plano submetido; listas vazias são válidas, objetivos não."""

    objectives: Annotated[ItemList, Field(min_length=1, max_length=MAX_OBJECTIVES)]
    scope: Annotated[ItemList, Field(max_length=MAX_SCOPE)]
    criteria: Annotated[list[Criterion], Field(max_length=MAX_CRITERIA)]
    gaps: Annotated[list[Gap], Field(max_length=MAX_GAPS)]
    out_of_scope: Annotated[ItemList, Field(max_length=MAX_OUT_OF_SCOPE)]
    charter: CharterInput
    revision_reason: ReasonField | None = None
    clarification_id: Annotated[str, Field(min_length=1, max_length=64)] | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        texts = [(item.text, item.source) for item in self.criteria]
        if len(set(texts)) != len(texts):
            raise ValueError("criteria must not repeat within a revision")
        gaps = [item.description for item in self.gaps]
        if len(set(gaps)) != len(gaps):
            raise ValueError("gaps must not repeat within a revision")
        if self.revision_reason == RevisionReason.CLARIFICATION:
            if self.clarification_id is None:
                raise ValueError("clarification revision requires clarification_id")
        elif self.clarification_id is not None:
            raise ValueError("clarification_id requires revision_reason 'clarification'")
        return self


type ActionId = Annotated[str, Field(min_length=1, max_length=64)]


class HeuristicCheckInput(_Strict):
    heuristic: HeuristicField
    criterion_id: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    outcome: OutcomeField
    action_ids: Annotated[list[ActionId], Field(min_length=1, max_length=MAX_ACTION_IDS)]
    note: str = Field(default="", max_length=MAX_NOTE_LENGTH)

    @model_validator(mode="after")
    def _unique_actions(self) -> Self:
        if len(set(self.action_ids)) != len(self.action_ids):
            raise ValueError("action_ids must not repeat")
        return self


type Option = Annotated[str, Field(max_length=MAX_OPTION_LENGTH), AfterValidator(_item)]


class ClarificationInput(_Strict):
    gap_id: Annotated[str, Field(min_length=1, max_length=64)]
    question: Annotated[str, Field(max_length=MAX_QUESTION_LENGTH), AfterValidator(_item)]
    why_it_matters: Annotated[str, Field(max_length=MAX_QUESTION_LENGTH), AfterValidator(_item)]
    options: Annotated[list[Option], Field(min_length=2, max_length=5)]


class ClarificationAnswer(_Strict):
    """Resposta do canal humano; informa o plano e nunca concede aprovação."""

    answer: Annotated[str, Field(max_length=MAX_ITEM_LENGTH), AfterValidator(_item)]
    scope: Literal["run", "project"] = "run"
