from typing import Any

import pytest

from lastro.evaluation.metrics import (
    AttemptMeasures,
    Resolved,
    aggregate,
    measure_attempt,
    pair_reduction,
    policy_violations,
    resolve,
    sample_stddev,
)
from lastro.history.evaluation import AttemptRecord, JudgmentRecord, ReviewRecord, TrialRecord


def finding(
    ref: str, *, confirmed: bool = False, verdict: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "ref": ref,
        "title": ref,
        "flow": "f",
        "steps": ["s"],
        "expected": "e",
        "observed": "o",
        "evidence": [],
        "classification": "confirmed" if confirmed else "suspected",
        "confirmation_source": "agent" if verdict is None else "reproduction",
        "verdict": verdict,
    }


def snapshot(**extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "findings": [],
        "criteria": [],
        "criteria_ids": ["c1", "c2"],
        "actions": {"total": 7, "by_tool": {"click": 4, "observe": 3}},
        "questions": [],
        "precondition_errors": {"exploration": 1},
        "approvals": [],
        "duration_seconds": 12.5,
        "usage": {"cost_nusd": None},
        "memory": {"stale_misuse": None},
        "access": {"intact": True, "control": 0, "deactivate": 0, "cross_tenant": 0, "writes": 0},
    }
    return {**base, **extra}


def value(measures: list[Any], metric: str) -> Any:
    return next(item for item in measures if item.metric == metric)


def match(bug: str) -> Resolved:
    return Resolved("match", bug, False)


NO = Resolved("no_match", None, False)
PENDING = Resolved(None, None, True)


def test_detection_deduplicates_catalog_ids() -> None:
    data = snapshot(findings=[finding("a"), finding("b"), finding("c")])
    measures = measure_attempt(
        data, ["d02", "d03", "d04", "d05"], {"a": match("D02"), "b": match("D02"), "c": NO}
    )
    detection = value(measures, "detection_rate")
    assert (detection.numerator, detection.denominator, detection.value) == (1, 4, 0.25)
    assert detection.complete


def test_stddev_is_sample_and_null_for_one() -> None:
    assert sample_stddev([1.0]) is None
    assert sample_stddev([]) is None
    assert sample_stddev([1.0, 3.0]) == pytest.approx(1.4142135, rel=1e-6)


def test_zero_denominator_is_null() -> None:
    measures = measure_attempt(snapshot(criteria_ids=[]), [], {})
    assert value(measures, "detection_rate").value is None
    assert value(measures, "detection_rate").unavailable_reason == "no_applicable_bugs"
    assert value(measures, "criteria_met_rate").value is None
    assert value(measures, "confirmed_precision").value is None
    assert value(measures, "reproduction_success").unavailable_reason == "no_reproduction"


def test_not_reproduced_is_not_false_positive() -> None:
    verdict = {"classification": "inconclusive", "reproduced_count": 0, "attempts_decisive": 3}
    data = snapshot(findings=[finding("a", verdict=verdict)])
    measures = measure_attempt(data, ["d02"], {"a": match("D02")})
    assert value(measures, "false_positive_rate").value is None
    assert value(measures, "false_positive_rate").unavailable_reason == "no_confirmed_findings"
    assert value(measures, "detection_rate").value == 1.0
    assert value(measures, "reproduction_success").value == 0.0


def test_pending_judge_does_not_become_negative() -> None:
    data = snapshot(findings=[finding("a"), finding("b")])
    measures = measure_attempt(data, ["d02", "d03"], {"a": match("D02"), "b": PENDING})
    detection = value(measures, "detection_rate")
    assert detection.value == 0.5 and not detection.complete
    unmatched = value(measures, "unmatched_finding_rate")
    assert (unmatched.numerator, unmatched.denominator) == (0, 1)
    judgment = JudgmentRecord(
        "j", "a", "r", "0" * 64, {}, "0" * 64, "judged", 1, "uncertain", None, "?", None, None, None
    )
    assert resolve(judgment, None).pending
    review = ReviewRecord("r", "i", 1, None, "ana", "match", "D02", "ok", "0" * 64)
    assert resolve(judgment, review) == match("D02")


def trial(trial_id: str, condition: str, status: str = "valid", **extra: Any) -> TrialRecord:
    data: dict[str, Any] = {
        "id": trial_id,
        "suite_id": "s",
        "ordinal": 1,
        "condition": condition,
        "scenario": "base",
        "variant": "base",
        "repetition": 1,
        "role": "measured",
        "parent_trial_id": None,
        "status": status,
        "condition_sha256": "0" * 64,
        "memory_sha256": None,
        "selected_attempt_id": f"att-{trial_id}",
    }
    return TrialRecord(**{**data, **extra})


def attempt(trial_id: str, executor: str = "claude-code", version: str = "1") -> AttemptRecord:
    return AttemptRecord(
        f"att-{trial_id}",
        trial_id,
        1,
        "valid",
        None,
        None,
        "x",
        {"name": executor, "version": version},
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )


def measured(trial_id: str, condition: str, detection: float, **kwargs: Any) -> AttemptMeasures:
    data = snapshot(findings=[finding("a")])
    resolved = {"a": match("D02") if detection else NO}
    return AttemptMeasures(
        trial(trial_id, condition),
        attempt(trial_id, **kwargs),
        measure_attempt(data, ["d02"], resolved),
        data,
    )


def test_invalid_trials_never_enter_n() -> None:
    trials = [trial("t1", "full"), trial("t2", "full"), trial("t3", "full", "invalid")]
    items = [measured("t1", "full", 1.0), measured("t2", "full", 0.0)]
    results = aggregate("tiny", "c" * 64, trials, items, {}, True, "f" * 64)
    detection = next(item for item in results if item.metric == "detection_rate")
    assert (detection.n, detection.planned_n, detection.invalid_n) == (2, 3, 1)
    assert detection.mean == 0.5
    assert detection.sample_stddev == pytest.approx(0.7071067, rel=1e-6)


def test_executor_strata_never_mix() -> None:
    trials = [trial("t1", "full"), trial("t2", "full")]
    items = [
        measured("t1", "full", 1.0, executor="claude-code"),
        measured("t2", "full", 0.0, executor="codex"),
    ]
    results = [
        item
        for item in aggregate("tiny", "c" * 64, trials, items, {}, True, "f" * 64)
        if item.metric == "detection_rate"
    ]
    assert len(results) == 2
    assert {item.stratum["executor"] for item in results} == {"claude-code", "codex"}
    assert all(item.n == 1 and item.sample_stddev is None for item in results)
    versions = [measured("t1", "full", 1.0, version="1"), measured("t2", "full", 1.0, version="2")]
    split = [
        item
        for item in aggregate("tiny", "c" * 64, trials, versions, {}, True, "f" * 64)
        if item.metric == "detection_rate"
    ]
    assert len(split) == 2


def test_pair_reduction_excludes_invalid_warmup() -> None:
    def pair(index: int, cold: float, warm: float, *, valid_warmup: bool = True) -> list[Any]:
        warm_trial = trial(f"w{index}", "full", parent_trial_id=f"c{index}")
        cold_trial = trial(f"c{index}", "full", role="warmup")
        cold_snapshot = snapshot(actions={"total": cold, "by_tool": {}})
        warm_snapshot = snapshot(actions={"total": warm, "by_tool": {}})
        items = [
            AttemptMeasures(
                warm_trial,
                attempt(warm_trial.id),
                measure_attempt(warm_snapshot, [], {}),
                warm_snapshot,
            )
        ]
        if valid_warmup:
            items.append(
                AttemptMeasures(
                    cold_trial,
                    attempt(cold_trial.id),
                    measure_attempt(cold_snapshot, [], {}),
                    cold_snapshot,
                )
            )
        return [warm_trial, cold_trial, items]

    trials: list[TrialRecord] = []
    items: list[AttemptMeasures] = []
    for index, (cold, warm, ok) in enumerate([(10, 5, True), (10, 8, True), (10, 1, False)]):
        warm_trial, cold_trial, entries = pair(index, cold, warm, valid_warmup=ok)
        trials += [warm_trial, cold_trial]
        items += entries
    (result,) = pair_reduction("mem", "c" * 64, trials, items, "f" * 64)
    assert result.n == 2 and result.planned_n == 3 and result.excluded_n == 1
    assert result.mean == pytest.approx(0.35)
    assert "incomplete_pairs" in result.limitations


def test_policy_violations_need_intact_log() -> None:
    assert policy_violations(snapshot(access={"intact": False})) == (None, "access_log_unavailable")
    data = snapshot(
        access={"intact": True, "control": 1, "deactivate": 1, "cross_tenant": 0, "writes": 3},
        approvals=[{"decision": "approve", "consumed": True}],
    )
    assert policy_violations(data) == (4, None)


def test_output_invalid_makes_output_metrics_unavailable() -> None:
    measures = measure_attempt(snapshot(output_error="output_invalid: x"), ["d02"], {})
    assert value(measures, "detection_rate").unavailable_reason == "output_invalid"
    assert value(measures, "actions").value == 7.0
    assert value(measures, "cost_usd").value is None
