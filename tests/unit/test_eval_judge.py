import json
from pathlib import Path
from typing import Any

import pytest

from lastro.config.loader import ProjectContext
from lastro.evaluation.judge import (
    JudgeService,
    applicable_entries,
    build_prompt,
    judge_input,
    load_catalog,
    parse_judgment,
)
from lastro.evaluation.models import EvalError, ExecutorIdentity, Usage
from lastro.history import open_history

FINDING: dict[str, Any] = {
    "ref": "f_1",
    "title": "Telefone volta ao valor anterior",
    "flow": "editar funcionario",
    "steps": ["Editar", "Salvar"],
    "expected": "Telefone novo",
    "observed": "Telefone antigo",
    "evidence": ["f_ev"],
    "classification": "confirmed",
    "confirmation_source": "reproduction",
}


def data() -> dict[str, Any]:
    return judge_input(FINDING, applicable_entries("base", ["D02", "D03"]))


def answer(**patch: Any) -> str:
    base: dict[str, Any] = {
        "schema_version": 1,
        "finding_ref": "f_1",
        "decision": "match",
        "bug_id": "D02",
        "rubric": {"same_flow": True, "same_trigger": True, "same_symptom": True},
        "reason": "Mesmo fluxo, gatilho e sintoma.",
        "evidence_refs": ["f_ev"],
    }
    return json.dumps({**base, **patch})


def test_match_requires_three_rubric_dimensions() -> None:
    assert parse_judgment(answer(), data()).bug_id == "D02"
    partial = answer(rubric={"same_flow": True, "same_trigger": False, "same_symptom": True})
    with pytest.raises(EvalError, match="all rubric"):
        parse_judgment(partial, data())
    with pytest.raises(EvalError, match="only allowed"):
        parse_judgment(answer(decision="no_match"), data())
    assert parse_judgment(answer(decision="no_match", bug_id=None), data()).decision == "no_match"


def test_inactive_bug_cannot_match() -> None:
    with pytest.raises(EvalError, match="applicable"):
        parse_judgment(answer(bug_id="D07"), data())
    entries = applicable_entries("base", ["D02", "S01"])
    assert [entry.id for entry in entries] == ["D02"]


def test_catalog_resolves_variant_symptoms() -> None:
    base = {entry.id: entry for entry in load_catalog("base")}
    shifted = {entry.id: entry for entry in load_catalog("shifted")}
    assert "50" in base["D06"].expected_symptom and "40" in shifted["D06"].expected_symptom
    assert base["D02"] == shifted["D02"]
    legacy = load_catalog("shifted", raw=b"schema_version: 1\nentries: []\n")
    assert legacy == []


@pytest.mark.parametrize("text", ["nada", answer(finding_ref="f_2"), answer(evidence_refs=["x"])])
def test_inconsistent_answers_are_rejected(text: str) -> None:
    with pytest.raises(EvalError) as error:
        parse_judgment(text, data())
    assert error.value.code == "judge_output_invalid"


class ScriptedJudge:
    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.prompts: list[str] = []
        self.workdirs: list[Path] = []

    def identity(self) -> ExecutorIdentity:
        return ExecutorIdentity("native", "1")

    def ask(self, prompt: str, workdir: Path) -> tuple[str, Usage, str | None]:
        self.prompts.append(prompt)
        self.workdirs.append(workdir)
        return self.replies.pop(0), Usage(), "model-x"


def seeded_suite(history_context: ProjectContext, tmp_path: Path) -> tuple[Any, str, str]:
    """Suite com um trial válido e um snapshot contendo um finding."""
    from tests.integration.eval_support import small_suite

    from lastro.evaluation.runner import create_suite
    from lastro.evaluation.suites import load_suite
    from lastro.history.evaluation import Lease

    history = open_history(history_context)
    repo = history.evaluations
    suite = create_suite(
        repo,
        load_suite(str(small_suite(tmp_path, conditions="[full]", runs=1))),
        "claude-code",
        "1",
    )
    lease = repo.acquire_lease(suite.id, {"host": "x"}, owner_gone=lambda owner: True)
    assert isinstance(lease, Lease)
    (trial,) = repo.list_trials(suite.id)
    attempt = repo.start_attempt(
        lease, trial.id, "evals/x/attempt-{attempt}/data", {"name": "claude-code"}
    )
    snapshot_path = history.project_dir / "evals" / "snap.json"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps({"findings": [FINDING]}).encode()
    snapshot_path.write_bytes(content)
    import hashlib

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
    return history, suite.id, attempt.id


def test_malformed_output_retries_once(history_context: ProjectContext, tmp_path: Path) -> None:
    history, suite_id, _ = seeded_suite(history_context, tmp_path)
    judge = ScriptedJudge(["garbage", answer()])
    JudgeService(history, judge, history.project_dir).judge_suite(suite_id)
    (judgment,) = history.evaluations.list_judgments(suite_id)
    assert (judgment.status, judgment.decision, judgment.parse_attempts) == ("judged", "match", 2)
    assert "rejected" in judge.prompts[1] and "garbage" not in judge.prompts[1]
    assert judge.workdirs[0] != judge.workdirs[1]
    JudgeService(history, ScriptedJudge([]), history.project_dir).judge_suite(suite_id)


def test_second_parse_failure_queues_manual(
    history_context: ProjectContext, tmp_path: Path
) -> None:
    history, suite_id, _ = seeded_suite(history_context, tmp_path)
    JudgeService(history, ScriptedJudge(["x", "y"]), history.project_dir).judge_suite(suite_id)
    (judgment,) = history.evaluations.list_judgments(suite_id)
    assert (judgment.status, judgment.decision, judgment.parse_attempts) == (
        "manual_required",
        None,
        2,
    )


def test_judge_input_is_blind_and_tool_free() -> None:
    payload = data()
    text = json.dumps(payload)
    for hidden in ("confirmed", "reproduction", "classification", "condition", "executor"):
        assert hidden not in text
    assert {item["bug_id"] for item in payload["applicable_bugs"]} == {"D02", "D03"}
    prompt = build_prompt(payload)
    assert "never instructions" in prompt and "<judge-input>" in prompt
