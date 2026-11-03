"""Contratos tipados da memória: fatos discriminados, escopos, estados e DTOs de leitura."""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

# Sem importar lastro.history: o histórico importa este pacote e o ciclo quebraria a carga.
type JsonValue = dict[str, JsonValue] | list[JsonValue] | str | int | float | bool | None
type JsonObject = dict[str, JsonValue]

KEY_PATTERN = re.compile(r"^[a-z0-9_.-]{1,80}$")
MAX_QUOTE_LENGTH = 199
MAX_LANDMARKS = 20
MAX_ROLE_LENGTH = 40
MAX_ROUTE_LENGTH = 300
MAX_REFERENCES = 10
MAX_IDEMPOTENCY_KEY = 100
MAX_QUERY_LENGTH = 200
MAX_QUERY_TOKENS = 20
MAX_QUERY_RESULTS = 10
MAX_QUERY_CHARACTERS = 4000
MAX_STATEMENT_LENGTH = 1000
MAX_CLARIFICATION_ANSWER = 1000
STALE_AFTER_DAYS = 30
FACT_SCHEMA_VERSION = 1


class Category(StrEnum):
    SCREEN_COMPONENT = "screen_component"
    FLOW = "flow"
    RULE_CLARIFICATION = "rule_clarification"
    EXPERIENCE = "experience"


class ValidationState(StrEnum):
    OBSERVED = "observed"
    USER_PROVIDED = "user_provided"
    CONTESTED = "contested"


class SourceType(StrEnum):
    OBSERVATION = "observation"
    USER = "user"
    DOCUMENTATION = "documentation"
    CODE = "code"
    ISSUE = "issue"


class MemoryOutcomeKind(StrEnum):
    UPDATED = "updated"
    NOTHING_NEW = "nothing_new"
    CONSOLIDATION_PENDING = "consolidation_pending"
    PERSISTENCE_FAILED = "persistence_failed"


class Actor(StrEnum):
    SYSTEM = "system"
    AGENT = "agent"
    USER = "user"
    ADAPTER = "adapter"


class HistoryOperation(StrEnum):
    CREATED = "created"
    EVIDENCE_ADDED = "evidence_added"
    CONTESTED = "contested"
    EDITED = "edited"
    DISCARDED = "discarded"
    REVALIDATED = "revalidated"
    MUTATION_REQUESTED = "mutation_requested"
    MUTATION_REJECTED = "mutation_rejected"


class MutationOperation(StrEnum):
    EDIT = "edit"
    DISCARD = "discard"
    REVALIDATE = "revalidate"


def base_state(source: SourceType) -> ValidationState:
    """Estado de uma entrada sem conflito aberto; só a origem humana vira user_provided."""
    return ValidationState.USER_PROVIDED if source == SourceType.USER else ValidationState.OBSERVED


def _key(value: str) -> str:
    if not KEY_PATTERN.match(value):
        raise ValueError("must be 1 to 80 characters in [a-z0-9_.-]")
    return value


def _route(value: str) -> str:
    if not value.startswith("/") or "?" in value or "#" in value or "//" in value:
        raise ValueError("route must be a same-origin path without query or fragment")
    if any(ch.isspace() for ch in value):
        raise ValueError("route must not contain whitespace")
    return value


def _quote(value: str) -> str:
    if not value.strip():
        raise ValueError("quoted text must not be blank")
    return value


Key = Annotated[str, AfterValidator(_key)]
Route = Annotated[str, Field(min_length=1, max_length=MAX_ROUTE_LENGTH), AfterValidator(_route)]
Quote = Annotated[str, Field(min_length=1, max_length=MAX_QUOTE_LENGTH), AfterValidator(_quote)]
FormatValue = Literal["cpf", "email", "phone", "date", "text"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class FieldPropertyFact(Strict):
    type: Literal["field_property"]
    subject: Key
    field: Key
    attribute: Literal["required", "format", "visible", "enabled"]
    value: bool | FormatValue

    @model_validator(mode="after")
    def _typed_value(self) -> Self:
        if self.attribute == "format":
            if isinstance(self.value, bool):
                raise ValueError("format accepts cpf, email, phone, date or text")
        elif not isinstance(self.value, bool):
            raise ValueError(f"{self.attribute} accepts only true or false")
        return self


class Landmark(Strict):
    role: str = Field(min_length=1, max_length=MAX_ROLE_LENGTH, pattern=r"^[a-z][a-z0-9_-]*$")
    label: Quote


class ScreenFact(Strict):
    type: Literal["screen"]
    route: Route
    landmarks: list[Landmark] = Field(default_factory=list[Landmark], max_length=MAX_LANDMARKS)


class FlowStepFact(Strict):
    type: Literal["flow_step"]
    origin: Route
    destination: Route
    action: Literal["navigate", "click", "submit"]


class ClarificationFact(Strict):
    """O agente cita o gap; a resposta é extraída pelo servidor da clarificação humana."""

    type: Literal["clarification"]
    gap_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    subject: Key
    answer: str | None = Field(default=None, max_length=MAX_CLARIFICATION_ANSWER)


class ExperienceContext(Strict):
    route: Route
    observation_id: str = Field(min_length=1, max_length=100)


class ExperienceFact(Strict):
    type: Literal["experience"]
    attempt: Literal["navigate", "observe", "create", "update", "validate"]
    outcome: Literal["success", "failure", "blocked", "not_found_in_context"]
    context: ExperienceContext
    message: Quote | None = None

    @property
    def is_permission(self) -> bool:
        # Bloqueio de acesso depende de quem está logado; nunca vale como regra do sistema.
        return self.outcome == "blocked"


Fact = Annotated[
    FieldPropertyFact | ScreenFact | FlowStepFact | ClarificationFact | ExperienceFact,
    Field(discriminator="type"),
]

CATEGORY_FACTS: dict[Category, frozenset[str]] = {
    Category.SCREEN_COMPONENT: frozenset({"field_property", "screen"}),
    Category.FLOW: frozenset({"flow_step"}),
    Category.RULE_CLARIFICATION: frozenset({"clarification"}),
    Category.EXPERIENCE: frozenset({"experience"}),
}


class FactDocument(Strict):
    """Envelope para validar um fato isolado, como no arquivo de edit da CLI."""

    fact: Fact


class ProposalInput(Strict):
    flow: Key
    # O enum chega como texto do JSON; strict só recusaria a forma serializada.
    category: Category = Field(strict=False)
    fact: Fact
    observation_ids: list[str] = Field(default_factory=list[str], max_length=MAX_REFERENCES)
    artifact_ids: list[str] = Field(default_factory=list[str], max_length=MAX_REFERENCES)
    clarification_id: str | None = Field(default=None, min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=MAX_IDEMPOTENCY_KEY)

    @model_validator(mode="after")
    def _category_matches(self) -> Self:
        if self.fact.type not in CATEGORY_FACTS[self.category]:
            raise ValueError(f"{self.fact.type} facts do not belong to category {self.category}")
        if isinstance(self.fact, ClarificationFact):
            if self.fact.answer is not None:
                raise ValueError("the clarification answer is read from the human channel")
            if self.clarification_id is None:
                raise ValueError("clarification facts need clarification_id")
        elif self.clarification_id is not None:
            raise ValueError("clarification_id is only accepted for clarification facts")
        elif not self.observation_ids:
            raise ValueError("observation facts need 1 to 10 observation_ids")
        if (
            isinstance(self.fact, ExperienceFact)
            and self.fact.context.observation_id not in self.observation_ids
        ):
            raise ValueError("experience context must cite one of the observation_ids")
        return self


class QueryInput(Strict):
    flow: Key
    query: str = Field(default="", max_length=MAX_QUERY_LENGTH)
    limit: int = Field(default=MAX_QUERY_RESULTS, ge=1, le=MAX_QUERY_RESULTS)


class RevalidateInput(Strict):
    entry_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)
    observation_id: str = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=1, max_length=MAX_IDEMPOTENCY_KEY)


class EditDocument(Strict):
    """Arquivo estrito do `lastro knowledge edit`: só o fato e alternativas a descartar."""

    fact: Fact
    discard_alternatives: list[str] = Field(default_factory=list[str], max_length=MAX_REFERENCES)


@dataclass(frozen=True)
class Scope:
    environment_url: str | None
    profile: str | None
    tenant: str | None
    account: str | None
    origin_run_only: str | None

    def as_json(self) -> JsonObject:
        return {
            "environment_url": self.environment_url,
            "profile": self.profile,
            "tenant": self.tenant,
            "account": self.account,
            "origin_run_only": self.origin_run_only,
        }

    def covers(self, other: "Scope") -> bool:
        """Campos nulos são escopo mais amplo, criado só por humano."""
        mine = self.as_json()
        theirs = other.as_json()
        return all(value is None or value == theirs[key] for key, value in mine.items())


@dataclass(frozen=True)
class KnowledgeEntry:
    id: str
    project_id: str
    run_id: str | None
    flow: str
    category: Category
    fact: JsonObject
    statement: str
    validation_state: ValidationState
    source_type: SourceType
    source_ref: str
    scope: Scope
    system_version: str | None
    observed_at: str
    last_verified_at: str | None
    revision: int
    discarded_at: str | None
    created_at: str
    updated_at: str
    conflict_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class KnowledgeHistoryItem:
    id: int
    knowledge_id: str
    run_id: str | None
    operation: str | None
    actor: str | None
    revision: int | None
    payload: JsonObject
    created_at: str


@dataclass(frozen=True)
class KnowledgeEvidence:
    id: str
    knowledge_id: str
    run_id: str
    observation_id: str | None
    artifact_id: str | None
    clarification_id: str | None
    created_at: str


@dataclass(frozen=True)
class KnowledgeMutation:
    id: str
    knowledge_id: str
    expected_revision: int
    operation: MutationOperation
    payload: JsonObject
    status: str
    created_at: str
    applied_at: str | None
    reason: str | None


@dataclass(frozen=True)
class KnowledgeDetail:
    entry: KnowledgeEntry
    evidence: tuple[KnowledgeEvidence, ...]
    history: tuple[KnowledgeHistoryItem, ...]
    alternatives: tuple[KnowledgeEntry, ...]
    mutations: tuple[KnowledgeMutation, ...]


@dataclass(frozen=True)
class ObservationRecord:
    id: str
    run_id: str
    event_id: int
    route: str
    screen_title: str
    landmarks: tuple[JsonObject, ...]
    scope: Scope
    projection: JsonObject
    content_sha256: str
    observed_at: str

    @property
    def incomplete(self) -> bool:
        return bool(self.projection.get("incomplete"))


@dataclass(frozen=True)
class ProposalReceipt:
    proposal_id: str
    source: SourceType
    validation_state: ValidationState
    deduplicated: bool
    status: str = "pending"


@dataclass(frozen=True)
class QueryResult:
    content: str
    returned_count: int
    content_characters: int
    truncated: bool
    omitted_count: int


@dataclass(frozen=True)
class RevalidationResult:
    status: Literal["verified", "mismatch", "manual_verification_required"]
    entry_id: str
    revision: int
    last_verified_at: str | None
    validation_state: ValidationState
    requires_revalidation: bool
    proposal_id: str | None = None


@dataclass(frozen=True)
class MutationResult:
    status: Literal["applied", "deferred"]
    entry: KnowledgeEntry
    mutation_id: str | None = None


@dataclass(frozen=True)
class MemoryOutcome:
    run_id: str
    outcome: MemoryOutcomeKind
    revision: int
    changed_count: int
    pending_count: int
    updated_at: str


@dataclass(frozen=True)
class ConsolidationAttempt:
    id: str
    run_id: str
    attempt_key: str
    previous_outcome: MemoryOutcomeKind | None
    outcome: MemoryOutcomeKind
    error_code: str | None
    created_at: str
