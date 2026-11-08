"""Resultado de uma tentativa a partir do relato do reprodutor e dos fatos da run filha.

O relato é só uma entrada: bloqueio por autenticação ou política, orçamento e oráculo são
verificados pelo servidor nos registros da run filha e da run explorada (regras 8 e 11 a 14).
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import cast

from lastro.execution.models import ACTION_LIMIT, DURATION_LIMIT
from lastro.history import (
    AttemptReason,
    EventRecord,
    EventType,
    PolicyDecision,
    ReproducerReport,
    ReproducerResult,
    ReproductionOutcome,
    RunStatus,
)
from lastro.oracles.models import OracleSignal
from lastro.reproduction.models import AttemptResult

type Signature = tuple[str, str, str]
type OracleIdentity = tuple[str, str, str, str]

AUTH_FAILURES = frozenset({"login_failed", "tenant_mismatch", "tenant_unverifiable"})
BUDGET_REASONS = frozenset({ACTION_LIMIT, DURATION_LIMIT})
_NUMERIC_SEGMENT = re.compile(r"/\d+(?=/|$)")


@dataclass(frozen=True)
class AttemptFacts:
    """Fatos da tentativa lidos do histórico; nada aqui vem do texto livre do agente."""

    ordinal: int
    preconditions: tuple[str, ...]
    child_status: RunStatus | None = None
    child_reason: str | None = None
    report: ReproducerReport | None = None
    child_artifact_ids: frozenset[str] = frozenset()
    auth_failed: bool = False
    denied_signatures: frozenset[Signature] = frozenset()
    allowed_signatures: frozenset[Signature] = frozenset()
    executor_available: bool = True
    executor_note: str = ""
    required_oracles: frozenset[OracleIdentity] | None = None
    child_oracles: frozenset[OracleIdentity] = field(default_factory=frozenset[OracleIdentity])


def signature(payload: object, tool_name: str | None) -> Signature | None:
    """Assinatura canônica (tool, operação, entidade) gravada na decisão de política."""
    if not isinstance(payload, dict):
        return None
    data = cast(dict[str, object], payload).get("signature")
    if not isinstance(data, dict):
        return None
    fields = cast(dict[str, object], data)
    tool = str(fields.get("tool") or tool_name or "")
    operation = str(fields.get("operation") or "")
    entity = str(fields.get("entity") or "")
    return (tool, operation, entity) if tool else None


def policy_signatures(events: Iterable[EventRecord], decision: PolicyDecision) -> set[Signature]:
    found: set[Signature] = set()
    for event in events:
        if event.event_type != EventType.POLICY_DECISION or event.policy_decision != decision:
            continue
        item = signature(event.payload, event.tool_name)
        if item is not None:
            found.add(item)
    return found


def authentication_failed(events: Iterable[EventRecord]) -> bool:
    for event in events:
        if event.event_type != EventType.AUTHENTICATION or not isinstance(event.payload, dict):
            continue
        if cast(dict[str, object], event.payload).get("subtype") in AUTH_FAILURES:
            return True
    return False


def oracle_identity(signal: OracleSignal) -> OracleIdentity:
    """Identidade da ocorrência entre runs: ids de registro variam, então viram {id}."""
    occurrence = signal.first_occurrence
    path = _NUMERIC_SEGMENT.sub("/{id}", signal.url_path)
    return (
        signal.signal_type,
        occurrence.http_method or "",
        str(occurrence.http_status or ""),
        path,
    )


def _inconclusive(
    facts: AttemptFacts,
    reason: AttemptReason,
    note: str = "",
    *,
    missing: str | None = None,
    index: int | None = None,
) -> AttemptResult:
    return AttemptResult(
        ordinal=facts.ordinal,
        outcome=ReproductionOutcome.INCONCLUSIVE,
        reason=reason,
        note=note,
        missing_precondition=missing,
        precondition_index=index,
    )


def interpret(facts: AttemptFacts) -> AttemptResult:
    if not facts.executor_available:
        return _inconclusive(facts, AttemptReason.EXECUTOR, facts.executor_note)
    report = facts.report
    note = report.note if report is not None else ""
    # Bloqueio vence até um relato de reprodução: o reprodutor não seguiu o caminho do explorador.
    if facts.auth_failed or facts.denied_signatures & facts.allowed_signatures:
        return _inconclusive(facts, AttemptReason.BLOCKED, note)
    if report is not None and report.result == ReproducerResult.REPRODUCED:
        evidence = report.evidence_artifact_ids
        if not evidence or not set(evidence) <= facts.child_artifact_ids:
            return _inconclusive(facts, AttemptReason.EVIDENCE, "evidence is not from this run")
        if facts.required_oracles is not None and not (
            facts.required_oracles & facts.child_oracles
        ):
            return _inconclusive(
                facts, AttemptReason.EVIDENCE, "the original oracle signal was not observed"
            )
        return AttemptResult(
            ordinal=facts.ordinal,
            outcome=ReproductionOutcome.REPRODUCED,
            reason=None,
            evidence_artifact_ids=evidence,
            note=note,
        )
    if facts.child_status == RunStatus.LIMIT_REACHED and facts.child_reason in BUDGET_REASONS:
        return _inconclusive(facts, AttemptReason.BUDGET, facts.child_reason or "")
    if report is None:
        status = facts.child_status.value if facts.child_status else "unknown"
        return _inconclusive(facts, AttemptReason.EVIDENCE, f"no result reported ({status})")
    if report.result == ReproducerResult.PRECONDITION_MISSING:
        index = report.precondition_index
        if index is None or not 0 <= index < len(facts.preconditions):
            return _inconclusive(facts, AttemptReason.EVIDENCE, "invalid precondition index")
        return _inconclusive(
            facts,
            AttemptReason.PRECONDITIONS,
            note,
            missing=facts.preconditions[index],
            index=index,
        )
    if report.result == ReproducerResult.DIVERGED:
        return _inconclusive(facts, AttemptReason.DIVERGED, note)
    return AttemptResult(
        ordinal=facts.ordinal,
        outcome=ReproductionOutcome.NOT_REPRODUCED,
        reason=None,
        note=note,
    )
