from lastro.history import AttemptReason, FindingClassification, ReproductionOutcome, VerdictReason
from lastro.reproduction import AttemptResult, decide

REPRODUCED = ReproductionOutcome.REPRODUCED
NOT_REPRODUCED = ReproductionOutcome.NOT_REPRODUCED
INCONCLUSIVE = ReproductionOutcome.INCONCLUSIVE


def hit(ordinal: int) -> AttemptResult:
    return AttemptResult(ordinal, REPRODUCED, None, evidence_artifact_ids=(f"art_{ordinal}",))


def miss(ordinal: int) -> AttemptResult:
    return AttemptResult(ordinal, NOT_REPRODUCED, None)


def stuck(ordinal: int, reason: AttemptReason, missing: str | None = None) -> AttemptResult:
    return AttemptResult(ordinal, INCONCLUSIVE, reason, missing_precondition=missing)


def verdict(*results: AttemptResult, planned: int = 2, verifier: str = "claude-code"):
    return decide(
        results,
        finding_id="finding_1",
        round_id="round_1",
        planned=planned,
        explorer="claude-code",
        verifier=verifier,
    )


def test_two_of_two_confirmed() -> None:
    result = verdict(hit(1), hit(2))
    assert result.classification == FindingClassification.CONFIRMED
    assert (result.reproduced_count, result.attempts_decisive) == (2, 2)
    assert result.label == "confirmed (2/2)"
    assert not result.intermittent and not result.cross_executor


def test_one_of_two_confirmed_with_frequency() -> None:
    result = verdict(hit(1), miss(2))
    assert result.label == "confirmed (1/2, intermittent)"
    assert result.intermittent
    assert result.notes == ("attempt 2: not reproduced",)


def test_two_of_three_label() -> None:
    result = verdict(hit(1), miss(2), hit(3), planned=3)
    assert result.label == "confirmed (2/3, intermittent)"
    assert result.attempts_planned == 3


def test_none_reproduced_inconclusive() -> None:
    result = verdict(miss(1), miss(2))
    assert result.classification == FindingClassification.INCONCLUSIVE
    assert result.reason == VerdictReason.NOT_REPRODUCED
    assert result.label == "not reproduced"
    assert len(result.notes) == 2


def test_blocked_has_priority_over_not_reproduced() -> None:
    result = verdict(miss(1), stuck(2, AttemptReason.BUDGET), stuck(3, AttemptReason.BLOCKED))
    assert result.reason == VerdictReason.BLOCKED
    assert result.label == "inconclusive (blocked)"
    assert "attempt 2: budget" in result.notes


def test_blocked_never_not_reproduced() -> None:
    result = verdict(stuck(1, AttemptReason.BLOCKED))
    assert result.reason == VerdictReason.BLOCKED
    assert result.attempts_decisive == 0


def test_non_decisive_attempts_listed() -> None:
    result = verdict(hit(1), stuck(2, AttemptReason.EXECUTOR), planned=2)
    assert result.label == "confirmed (1/1)"
    assert result.notes == ("attempt 2: executor",)


def test_preconditions_names_the_item() -> None:
    result = verdict(stuck(1, AttemptReason.PRECONDITIONS, "Funcionário com CPF 123 existe"))
    assert result.label == "inconclusive (preconditions)"
    assert result.missing_precondition == "Funcionário com CPF 123 existe"


def test_confirmed_requires_reproduced_with_evidence() -> None:
    claimed = AttemptResult(1, REPRODUCED, None)
    result = verdict(claimed, miss(2))
    assert result.classification == FindingClassification.INCONCLUSIVE
    assert result.reason == VerdictReason.EVIDENCE
    assert result.reproduced_count == 0


def test_unavailable_executor_without_attempts() -> None:
    result = decide(
        [],
        finding_id="finding_1",
        round_id="round_1",
        planned=2,
        explorer="claude-code",
        verifier="codex",
        executor_note="npm install -g @openai/codex",
    )
    assert result.label == "inconclusive (executor)"
    assert result.cross_executor
    assert result.notes == ("npm install -g @openai/codex",)
