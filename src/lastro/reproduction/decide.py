"""Veredito puro a partir das tentativas interpretadas (regras 9 e 10)."""

from collections.abc import Sequence

from lastro.history import (
    AttemptReason,
    FindingClassification,
    ReproductionOutcome,
    VerdictInput,
    VerdictReason,
)
from lastro.reproduction.models import AttemptResult

# Prioridade do motivo quando nenhuma tentativa reproduziu: a de maior prioridade vence.
REASON_PRIORITY = (
    AttemptReason.BLOCKED,
    AttemptReason.PRECONDITIONS,
    AttemptReason.BUDGET,
    AttemptReason.EXECUTOR,
    AttemptReason.DIVERGED,
    AttemptReason.EVIDENCE,
)


def _counted(result: AttemptResult) -> AttemptResult:
    """Reproduzido sem evidência da run filha nunca conta: vira não decisivo por evidência."""
    if result.outcome == ReproductionOutcome.REPRODUCED and not result.evidence_artifact_ids:
        return AttemptResult(
            ordinal=result.ordinal,
            outcome=ReproductionOutcome.INCONCLUSIVE,
            reason=AttemptReason.EVIDENCE,
            note=result.note,
        )
    return result


def _note(result: AttemptResult) -> str:
    if result.outcome == ReproductionOutcome.NOT_REPRODUCED:
        text = f"attempt {result.ordinal}: not reproduced"
    elif result.reason is not None:
        text = f"attempt {result.ordinal}: {result.reason.value}"
        if result.missing_precondition:
            text += f" ({result.missing_precondition})"
    else:
        text = f"attempt {result.ordinal}: reproduced"
    return f"{text}: {result.note}" if result.note else text


def decide(
    results: Sequence[AttemptResult],
    *,
    finding_id: str,
    round_id: str,
    planned: int,
    explorer: str,
    verifier: str,
    executor_note: str | None = None,
) -> VerdictInput:
    """Confirmado só com ao menos uma reprodução com evidência própria; nunca por declaração."""
    counted = [_counted(item) for item in results]
    reproduced = sum(1 for item in counted if item.outcome == ReproductionOutcome.REPRODUCED)
    decisive = sum(1 for item in counted if item.decisive)
    pending = [item for item in counted if not item.decisive]
    notes = [_note(item) for item in counted if item.outcome != ReproductionOutcome.REPRODUCED]
    if executor_note:
        notes.append(executor_note)
    reason: VerdictReason | None = None
    missing: str | None = None
    if reproduced >= 1:
        classification = FindingClassification.CONFIRMED
    else:
        classification = FindingClassification.INCONCLUSIVE
        if not counted:
            reason = VerdictReason.EXECUTOR if executor_note else VerdictReason.EVIDENCE
        elif not pending:
            reason = VerdictReason.NOT_REPRODUCED
        else:
            reasons = {item.reason for item in pending}
            chosen = next(item for item in REASON_PRIORITY if item in reasons)
            reason = VerdictReason(chosen.value)
            if chosen == AttemptReason.PRECONDITIONS:
                missing = next(
                    item.missing_precondition
                    for item in pending
                    if item.reason == AttemptReason.PRECONDITIONS
                )
    return VerdictInput(
        finding_id=finding_id,
        round_id=round_id,
        attempts_planned=max(planned, len(counted), 1),
        attempts_decisive=decisive,
        reproduced_count=reproduced,
        classification=classification,
        reason=reason,
        missing_precondition=missing,
        intermittent=0 < reproduced < decisive,
        cross_executor=verifier != explorer,
        explorer_executor=explorer,
        verifier_executor=verifier,
        notes=tuple(notes),
    )
