import json
import math
from pathlib import Path
from typing import Any

import pytest

from lastro.config.loader import ProjectContext
from lastro.evaluation.models import EvalError
from lastro.evaluation.review import (
    ensure_sample,
    export_review,
    import_reviews,
    review_status,
    sample_size,
    select_sample,
)
from lastro.history import HistoryStore, open_history

from .test_eval_judge import FINDING


def seeded(
    history_context: ProjectContext, tmp_path: Path, decisions: list[str]
) -> tuple[HistoryStore, str]:
    """Suite com um trial válido e um julgamento por finding, já decididos."""
    import hashlib

    from tests.integration.eval_support import small_suite

    from lastro.evaluation.judge import applicable_entries, input_sha256, judge_input
    from lastro.evaluation.runner import create_suite
    from lastro.evaluation.suites import load_suite

    history = open_history(history_context)
    repo = history.evaluations
    suite = create_suite(
        repo,
        load_suite(str(small_suite(tmp_path, conditions="[full]", runs=1))),
        "claude-code",
        "1",
    )
    lease = repo.acquire_lease(suite.id, {}, owner_gone=lambda owner: True)
    (trial,) = repo.list_trials(suite.id)
    attempt = repo.start_attempt(lease, trial.id, "evals/x/{attempt}", {"name": "claude-code"})
    findings = [{**FINDING, "ref": f"f_{index}"} for index in range(len(decisions))]
    content = json.dumps({"findings": findings}).encode()
    path = history.project_dir / "evals" / "snap.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    repo.finish_attempt(
        lease,
        attempt.id,
        status="valid",
        reason="completed",
        trial_status="valid",
        snapshot_relative_path="evals/snap.json",
        snapshot_sha256=hashlib.sha256(content).hexdigest(),
        select=True,
    )
    entries = applicable_entries("base", ["D02"])
    for finding, decision in zip(findings, decisions, strict=True):
        data = judge_input(finding, entries)
        record = repo.ensure_judgment(attempt.id, finding["ref"], input_sha256(data), {}, "0" * 64)
        status = "manual_required" if decision == "manual" else "judged"
        repo.record_judgment(
            record.id,
            status=status,
            parse_attempts=1,
            decision=None if decision == "manual" else decision,
            bug_id="D02" if decision == "match" else None,
            reason="r",
        )
    return history, suite.id


@pytest.mark.parametrize(("m", "expected"), [(0, 0), (1, 1), (5, 1), (6, 2), (10, 2), (11, 3)])
def test_sample_is_ceil_twenty_percent_without_replacement(m: int, expected: int) -> None:
    assert sample_size(m, 0.2) == expected == math.ceil(round(0.2 * m, 9))
    ids = [f"j{index}" for index in range(m)]
    sample = select_sample(ids, 0.2, seed=7)
    assert len(sample) == len(set(sample)) == expected
    assert select_sample(list(reversed(ids)), 0.2, seed=7) == sample


def test_sample_is_frozen_once(history_context: ProjectContext, tmp_path: Path) -> None:
    history, suite_id = seeded(
        history_context, tmp_path, ["match", "no_match", "match", "uncertain", "manual"]
    )
    first = ensure_sample(history, suite_id)
    assert first.sample_required == 1 and first.extra_pending == 2
    items = history.evaluations.review_items(suite_id)
    assert ensure_sample(history, suite_id) == first
    assert history.evaluations.review_items(suite_id) == items


def test_review_export_hides_judge_and_condition(
    history_context: ProjectContext, tmp_path: Path
) -> None:
    history, suite_id = seeded(history_context, tmp_path, ["match", "no_match"])
    path = export_review(history, suite_id, history.project_dir)
    text = path.read_text(encoding="utf-8")
    lines = [json.loads(line) for line in text.splitlines()]
    assert lines and all(line["decision"] is None for line in lines)
    for hidden in ("full", "claude-code", "match", "judge", "condition"):
        assert f'"{hidden}"' not in text
    assert history.project_dir in path.parents


def answer(line: dict[str, Any], decision: str, bug: str | None = None) -> str:
    return json.dumps({**line, "decision": decision, "bug_id": bug, "reason": "conferido"})


def test_agreement_uses_original_decision(history_context: ProjectContext, tmp_path: Path) -> None:
    history, suite_id = seeded(history_context, tmp_path, ["match"] * 10)
    path = export_review(history, suite_id, history.project_dir)
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    filled = tmp_path / "filled.jsonl"
    filled.write_text(
        "\n".join([answer(lines[0], "match", "D02"), answer(lines[1], "no_match")]),
        encoding="utf-8",
    )
    assert import_reviews(history, suite_id, filled, "ana") == (2, 0)
    status = review_status(history, suite_id)
    assert (status.agreement_numerator, status.agreement_denominator) == (1, 2)
    assert status.agreement == 0.5 and status.complete
    (judgment,) = {item.decision for item in history.evaluations.list_judgments(suite_id)}
    assert judgment == "match"


def test_review_import_is_atomic_and_idempotent(
    history_context: ProjectContext, tmp_path: Path
) -> None:
    history, suite_id = seeded(history_context, tmp_path, ["match"] * 10)
    path = export_review(history, suite_id, history.project_dir)
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    bad = tmp_path / "bad.jsonl"
    forged = {**lines[1], "input_sha256": "f" * 64}
    bad.write_text(
        "\n".join([answer(lines[0], "match", "D02"), answer(forged, "match", "D02")]),
        encoding="utf-8",
    )
    with pytest.raises(EvalError) as error:
        import_reviews(history, suite_id, bad, "ana")
    assert error.value.code == "review_manifest_mismatch"
    assert history.evaluations.all_reviews(suite_id) == []
    good = tmp_path / "good.jsonl"
    good.write_text(answer(lines[0], "match", "D02"), encoding="utf-8")
    assert import_reviews(history, suite_id, good, "ana") == (1, 0)
    assert import_reviews(history, suite_id, good, "ana") == (0, 1)
    with pytest.raises(EvalError):
        import_reviews(history, suite_id, good, " ")


def test_correction_creates_revision(history_context: ProjectContext, tmp_path: Path) -> None:
    history, suite_id = seeded(history_context, tmp_path, ["match"] * 3)
    path = export_review(history, suite_id, history.project_dir)
    (line,) = [json.loads(item) for item in path.read_text(encoding="utf-8").splitlines()]
    first = tmp_path / "first.jsonl"
    first.write_text(answer(line, "match", "D02"), encoding="utf-8")
    second = tmp_path / "second.jsonl"
    second.write_text(answer(line, "no_match"), encoding="utf-8")
    import_reviews(history, suite_id, first, "ana")
    import_reviews(history, suite_id, second, "ana")
    reviews = history.evaluations.all_reviews(suite_id)
    assert [item.revision for item in reviews] == [1, 2]
    assert reviews[1].previous_review_id == reviews[0].id
    assert review_status(history, suite_id).agreement == 0.0
