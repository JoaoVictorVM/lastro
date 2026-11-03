"""Decisão determinística de consolidação: deduplicar, criar, contestar ou pedir revisão.

Não há modelo de linguagem aqui. Igualdade é de escopo canônico, chave factual e hash do valor;
textos equivalentes só por semântica nunca são fundidos automaticamente.
"""

from dataclasses import dataclass
from typing import Literal

from lastro.knowledge.models import (
    MAX_CLARIFICATION_ANSWER,
    ClarificationFact,
    Fact,
    MemoryOutcomeKind,
)

REDACTED_MARK = "[REDACTED]"


@dataclass(frozen=True)
class ActiveValue:
    entry_id: str
    value_sha256: str


@dataclass(frozen=True)
class Decision:
    kind: Literal["deduplicate", "create", "review"]
    entry_id: str | None = None
    conflicts_with: tuple[str, ...] = ()
    reason: str | None = None


def review_reason(fact: Fact) -> str | None:
    """Fato válido que ainda não é comparável sem um humano; fica pendente, nunca é comprimido."""
    if isinstance(fact, ClarificationFact):
        answer = (fact.answer or "").strip()
        if not answer or answer == REDACTED_MARK:
            return "clarification_answer_not_representable"
        if len(answer) > MAX_CLARIFICATION_ANSWER:
            return "clarification_answer_too_long"
    return None


def decide(fact: Fact, value_sha256: str, actives: list[ActiveValue]) -> Decision:
    """Mesmo valor acrescenta evidência; valor diferente cria alternativa contestada."""
    reason = review_reason(fact)
    if reason is not None:
        return Decision("review", reason=reason)
    for active in actives:
        if active.value_sha256 == value_sha256:
            return Decision("deduplicate", entry_id=active.entry_id)
    return Decision("create", conflicts_with=tuple(sorted(a.entry_id for a in actives)))


def outcome(*, changed: int, pending: int, persistence_failed: bool = False) -> MemoryOutcomeKind:
    """Precedência: persistence_failed, depois pendências, depois updated."""
    if persistence_failed:
        return MemoryOutcomeKind.PERSISTENCE_FAILED
    if pending > 0:
        return MemoryOutcomeKind.CONSOLIDATION_PENDING
    if changed > 0:
        return MemoryOutcomeKind.UPDATED
    return MemoryOutcomeKind.NOTHING_NEW
