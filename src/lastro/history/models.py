"""Contratos tipados do histórico: estados, eventos, missões, registros e filtros."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path

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


# Somente o registro abre e fecha a run; consumidores não anexam esses dois tipos.
LIFECYCLE_EVENTS = frozenset({EventType.RUN_STARTED, EventType.RUN_ENDED})
CORRELATED_EVENTS = frozenset({EventType.TOOL_CALL, EventType.POLICY_DECISION})


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


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
