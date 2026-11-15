"""Contratos da avaliação: schema estrito das suites, estados, slots e a porta headless."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any, Literal, cast

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictBool, StrictInt

SCHEMA_VERSION = 1
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_SUITE_BYTES = 1024 * 1024
MAX_MISSION_CHARS = 10_000
DEFAULT_RUNS = 5
DEFAULT_SAMPLE_FRACTION = 0.20
DEFAULT_SUITE_BUDGET_USD = "30.00"
EXECUTORS = ("claude-code", "codex", "native")
DEMO_DEFECTS = tuple(f"d{number:02d}" for number in range(1, 11))
DEMO_SCENARIOS = ("s01", "s02")
DEMO_VARIANTS = ("base", "shifted")

USAGE_EXIT_CODE = 2
PAUSED_EXIT_CODE = 4
INTERRUPTED_EXIT_CODE = 130


def as_map(value: object) -> dict[str, Any]:
    """Mapeamento JSON tipado; qualquer outro valor vira mapa vazio."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def as_list(value: object) -> list[Any]:
    return cast(list[Any], value) if isinstance(value, list) else []


class EvalError(Exception):
    """Erro tipado da avaliação; `code` é estável e aparece na CLI e nos eventos."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class Condition(StrEnum):
    BASELINE = "baseline"
    FULL = "full"
    NO_HEURISTICS = "-heuristics"
    NO_ORACLES = "-oracles"
    NO_REPRODUCTION = "-reproduction"
    NO_MEMORY = "-memory"


ALL_CONDITIONS = tuple(Condition)


class SuiteStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    AWAITING_REVIEW = "awaiting_review"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TrialStatus(StrEnum):
    PENDING = "pending"
    PREPARING = "preparing"
    RUNNING = "running"
    COLLECTING = "collecting"
    VALID = "valid"
    INVALID = "invalid"
    PAUSED = "paused"
    NOT_APPLICABLE = "not_applicable"

    @property
    def settled(self) -> bool:
        return self in {TrialStatus.VALID, TrialStatus.INVALID, TrialStatus.NOT_APPLICABLE}


class AttemptStatus(StrEnum):
    RUNNING = "running"
    VALID = "valid"
    INVALID = "invalid"
    PAUSED = "paused"


class JudgmentStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    JUDGED = "judged"
    MANUAL_REQUIRED = "manual_required"
    REVIEWED = "reviewed"


class TrialRole(StrEnum):
    MEASURED = "measured"
    WARMUP = "warmup"


class InvalidReason(StrEnum):
    RESET = "reset"
    VERSION = "version"
    CRASH = "crash"
    ISOLATION = "isolation"


class StopReason(StrEnum):
    COMPLETED = "completed"
    BLOCKED = "blocked"
    INTERRUPTED = "interrupted"
    RATE_LIMIT = "rate_limit"
    USAGE_CAP = "usage_cap"
    COST_CAP = "cost_cap"
    CRASH = "crash"

    @property
    def pauses_suite(self) -> bool:
        return self in {StopReason.RATE_LIMIT, StopReason.USAGE_CAP, StopReason.COST_CAP}


class TaskPurpose(StrEnum):
    EVALUATION = "evaluation"
    RETRIEVAL = "retrieval"
    AUTONOMOUS = "autonomous"


class GateMode(StrEnum):
    SCRIPTED = "scripted"
    UNATTENDED = "unattended"


class JudgeDecision(StrEnum):
    MATCH = "match"
    NO_MATCH = "no_match"
    UNCERTAIN = "uncertain"


class MemoryMode(StrEnum):
    EMPTY = "empty"
    FIXTURE = "fixture"
    PAIRED = "paired"


class BudgetStatus(StrEnum):
    RESERVED = "reserved"
    SENT = "sent"
    SETTLED = "settled"
    UNCERTAIN = "uncertain"
    RELEASED = "released"


def _safe_id(value: str) -> str:
    if not SAFE_ID.fullmatch(value):
        raise ValueError("id deve seguir [a-z0-9][a-z0-9-]{0,63}")
    return value


def _non_empty(value: str) -> str:
    if not value.strip():
        raise ValueError("texto vazio")
    return value


SafeId = Annotated[str, AfterValidator(_safe_id)]
Text = Annotated[str, Field(min_length=1, max_length=2000), AfterValidator(_non_empty)]


def _unique(values: list[str], label: str) -> list[str]:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} repetido")
    return values


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class CriterionSpec(_Strict):
    id: SafeId
    text: Text


class MissionSpec(_Strict):
    id: SafeId
    file: Text
    criteria: tuple[CriterionSpec, ...] = ()

    def model_post_init(self, context: Any) -> None:
        _unique([item.id for item in self.criteria], "critério")


class DemoSpec(_Strict):
    defects: tuple[str, ...] = DEMO_DEFECTS
    scenarios: tuple[str, ...] = DEMO_SCENARIOS
    variant: Literal["base", "shifted"] = "base"
    applicable_defects: tuple[str, ...] | None = None

    def model_post_init(self, context: Any) -> None:
        _unique(list(self.defects), "defeito")
        _unique(list(self.scenarios), "cenário do demo")
        unknown = [item for item in self.defects if item not in DEMO_DEFECTS]
        unknown += [item for item in self.scenarios if item not in DEMO_SCENARIOS]
        if unknown:
            raise ValueError("flags desconhecidas: " + ", ".join(unknown))
        for item in self.applicable_defects or ():
            if item not in self.defects:
                raise ValueError(f"defeito aplicável {item} não está habilitado")

    @property
    def measured_defects(self) -> tuple[str, ...]:
        return self.applicable_defects if self.applicable_defects is not None else self.defects


class MemorySpec(_Strict):
    mode: MemoryMode = MemoryMode.EMPTY
    fixture: str | None = None
    fixture_sha256: str | None = None
    scope: str | None = None
    stale_days: StrictInt | None = Field(default=None, ge=0, le=3650)
    stale_version: str | None = None

    def model_post_init(self, context: Any) -> None:
        if self.mode == MemoryMode.FIXTURE:
            if not self.fixture or not self.fixture_sha256 or not self.scope:
                raise ValueError("fixture exige arquivo, hash e escopo")
            if not SHA256.fullmatch(self.fixture_sha256):
                raise ValueError("fixture_sha256 inválido")
        elif self.fixture or self.fixture_sha256:
            raise ValueError("arquivo de memória só com mode=fixture")


class ApproverRule(_Strict):
    id: SafeId
    tenant: Text
    operation: Literal["create", "update"]
    route: Text
    fields: tuple[str, ...]
    values: Mapping[str, str] = Field(default_factory=dict[str, str])
    decision: Literal["approve", "skip"]
    max_uses: StrictInt = Field(default=1, ge=1, le=10)

    def model_post_init(self, context: Any) -> None:
        _unique(list(self.fields), "campo")
        extra = [name for name in self.values if name not in self.fields]
        if extra:
            raise ValueError("valores para campos fora da regra: " + ", ".join(extra))


class ApproverSpec(_Strict):
    default: Literal["skip"] = "skip"
    rules: tuple[ApproverRule, ...] = ()

    def model_post_init(self, context: Any) -> None:
        _unique([rule.id for rule in self.rules], "regra")


class ClarificationSpec(_Strict):
    gap_key: SafeId
    answer: str | None = Field(default=None, max_length=2000)
    source: Literal["synthetic"] = "synthetic"


class InjectionSpec(_Strict):
    kind: Literal["interrupt", "block"]
    after_actions: StrictInt = Field(default=3, ge=1, le=100)


class ScenarioSpec(_Strict):
    id: SafeId
    mission: SafeId
    conditions: tuple[Condition, ...] | None = None
    demo: DemoSpec
    memory: MemorySpec = MemorySpec()
    approver: ApproverSpec = ApproverSpec()
    clarifications: tuple[ClarificationSpec, ...] = ()
    injection: InjectionSpec | None = None
    tenant_mismatch: StrictBool = False
    requires: tuple[Literal["gates", "tenant", "memory"], ...] = ()

    def model_post_init(self, context: Any) -> None:
        _unique([item.gap_key for item in self.clarifications], "lacuna")
        if self.conditions is not None:
            _unique(list(self.conditions), "condição do cenário")


class JudgeSpec(_Strict):
    executor: Literal["claude-code", "codex", "native"] | None = None
    model: str | None = None
    sample_fraction: float = Field(default=DEFAULT_SAMPLE_FRACTION, ge=0.0, le=1.0)
    parse_retries: Literal[1] = 1


class LimitsSpec(_Strict):
    max_actions: StrictInt | None = Field(default=None, ge=1, le=10_000)
    max_minutes: StrictInt | None = Field(default=None, ge=1, le=600)
    suite_budget_usd: str = DEFAULT_SUITE_BUDGET_USD

    def model_post_init(self, context: Any) -> None:
        if not re.fullmatch(r"\d{1,6}(\.\d{1,2})?", self.suite_budget_usd):
            raise ValueError("suite_budget_usd deve ser decimal com até 2 casas")


class SuiteSpec(_Strict):
    schema_version: Literal[1]
    id: SafeId
    purpose: TaskPurpose = TaskPurpose.EVALUATION
    executor: Literal["claude-code", "codex", "native"] = "claude-code"
    model: str | None = None
    runs_per_condition: StrictInt = Field(default=DEFAULT_RUNS, ge=1, le=100)
    seed: StrictInt = Field(default=0, ge=0, le=2_147_483_647)
    conditions: tuple[Condition, ...]
    missions: tuple[MissionSpec, ...]
    scenarios: tuple[ScenarioSpec, ...]
    pairing: Literal["none", "warmup"] = "none"
    judge: JudgeSpec = JudgeSpec()
    limits: LimitsSpec = LimitsSpec()

    def model_post_init(self, context: Any) -> None:
        if not self.conditions:
            raise ValueError("conditions vazio")
        if not self.missions or not self.scenarios:
            raise ValueError("missions e scenarios são obrigatórios")
        _unique(list(self.conditions), "condição")
        _unique([item.id for item in self.missions], "missão")
        _unique([item.id for item in self.scenarios], "cenário")
        missions = {item.id for item in self.missions}
        for scenario in self.scenarios:
            if scenario.mission not in missions:
                raise ValueError(f"cenário {scenario.id} usa missão desconhecida")
            for condition in scenario.conditions or ():
                if condition not in self.conditions:
                    raise ValueError(f"cenário {scenario.id} usa condição fora da suite")
        if self.pairing == "warmup" and Condition.FULL not in self.conditions:
            raise ValueError("pairing warmup exige a condição full")

    def mission(self, mission_id: str) -> MissionSpec:
        return next(item for item in self.missions if item.id == mission_id)


@dataclass(frozen=True)
class TrialSlot:
    """Posição congelada da suite: a ordem e o par de aquecimento nunca mudam no resume."""

    ordinal: int
    condition: Condition
    scenario: str
    variant: str
    repetition: int
    role: TrialRole
    applicable: bool
    parent_ordinal: int | None = None
    demo_seed: int = 0

    @property
    def key(self) -> tuple[str, str, str, int, str]:
        return (self.condition.value, self.scenario, self.variant, self.repetition, self.role.value)


@dataclass(frozen=True)
class ExecutorIdentity:
    name: str
    version: str = "unknown"
    model_requested: str | None = None
    model_observed: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "model_requested": self.model_requested,
            "model_observed": self.model_observed or "unknown",
        }


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_nusd: int | None = None
    cost_source: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_nusd": self.cost_nusd,
            "cost_source": self.cost_source,
        }


@dataclass(frozen=True)
class HeadlessTask:
    """Pedido interno do supervisor; o agente nunca escolhe executável, alvo ou gate mode."""

    task_id: str
    purpose: TaskPurpose
    executor: str
    model: str | None
    workspace: Any
    mission_ref: str
    gate_mode: GateMode
    budget_nusd: int | None = None
    evaluation: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.purpose == TaskPurpose.AUTONOMOUS and self.gate_mode != GateMode.UNATTENDED:
            raise EvalError("invalid_task", "purpose=autonomous exige gate_mode=unattended")
        if self.purpose != TaskPurpose.EVALUATION and self.evaluation is not None:
            raise EvalError("invalid_task", "contexto de avaliação só em purpose=evaluation")
        if self.executor not in EXECUTORS:
            raise EvalError("executor_unavailable", f"executor desconhecido: {self.executor}")


@dataclass(frozen=True)
class HeadlessResult:
    status: StopReason
    executor: ExecutorIdentity
    run_ref: str | None = None
    snapshot_ref: str | None = None
    snapshot_sha256: str | None = None
    usage: Usage = Usage()
    logs: tuple[str, ...] = ()
    output: Mapping[str, Any] | None = None
    output_error: str | None = None
    reproduction_usage: Usage | None = None
    events: tuple[Mapping[str, Any], ...] = field(default_factory=tuple[Mapping[str, Any], ...])
