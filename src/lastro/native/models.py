"""Estado transitório do grafo e resultados do runner; nada aqui é fonte de verdade."""

from dataclasses import dataclass, field
from typing import Any, TypedDict

GRAPH_RECURSION_LIMIT = 512
MAX_INVALID_RESPONSES = 3
PREPARATION_TOOLS = frozenset(
    {"mission_context", "submit_mission_plan", "ask_clarification", "approval_status", "end_run"}
)
MEMORY_TOOLS = frozenset({"knowledge_query", "knowledge_revalidate", "end_run"})
EFFECT_TOOLS = frozenset({"navigate", "click", "fill", "select", "submit"})
RECORD_TOOLS = frozenset(
    {
        "report_finding",
        "record_criterion_result",
        "record_test_check",
        "knowledge_propose",
        "report_reproduction_result",
    }
)
REFERENCE_KEYS = ("finding_id", "result_id", "check_id", "proposal_id", "plan_id")


class NativeState(TypedDict, total=False):
    """Mensagens e filas do loop; cliente, chave e conexões ficam nas dependências."""

    messages: list[dict[str, Any]]
    pending_user: list[dict[str, Any]]
    queue: list[dict[str, Any]]
    last_tools: list[str]
    seen_tool_ids: list[str]
    plan_accepted: bool
    memory_consulted: bool
    invalid_streak: int
    step: int
    references: list[dict[str, Any]]
    terminal: tuple[str, str] | None
    ended_by_agent: dict[str, Any] | None


class TerminalSignal(Exception):
    """Interrompe o grafo e leva ao encerramento confiável sem nova geração."""

    def __init__(self, status: str, reason: str) -> None:
        super().__init__(f"{status} ({reason})")
        self.status, self.reason = status, reason


@dataclass(frozen=True)
class UsageSummary:
    calls: int
    uncertain_calls: int
    input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    confirmed_usd: str
    committed_usd: str


@dataclass
class NativeOutcome:
    run_id: str
    status: str
    reason: str | None
    report_paths: list[str] = field(default_factory=list[str])
    report_status: str | None = None
    usage: UsageSummary | None = None
    detail: str | None = None
