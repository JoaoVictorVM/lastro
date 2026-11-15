import json
from pathlib import Path

import pytest

from lastro.config.loader import ProjectContext
from lastro.evaluation import executors
from lastro.evaluation.decisions import ScriptedApprover, TrialDecisions
from lastro.evaluation.executors import (
    ClaudeCodeExecutor,
    CodexExecutor,
    HeadlessExecutor,
    NativeExecutor,
    default_executor,
    default_judge,
    parse_claude_output,
)
from lastro.evaluation.models import (
    EXECUTORS,
    ApproverRule,
    ApproverSpec,
    EvalError,
    GateMode,
    HeadlessTask,
    StopReason,
    TaskPurpose,
)
from lastro.evaluation.runner import create_suite, persist_task_metrics
from lastro.evaluation.suites import load_suite
from lastro.history import ApprovalStatus, TextMission, open_history
from lastro.history.evaluation import MeasurementInput

from .eval_support import small_suite


def test_three_executors_return_same_task_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    built = [default_executor(name, None, lambda line: None) for name in EXECUTORS]
    assert [type(item) for item in built] == [ClaudeCodeExecutor, CodexExecutor, NativeExecutor]
    for item in built:
        assert isinstance(item, HeadlessExecutor)
    monkeypatch.setattr(executors.shutil, "which", lambda name: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(EvalError) as error:
        ClaudeCodeExecutor().identity()
    assert error.value.code == "executor_unavailable"
    with pytest.raises(EvalError) as error:
        NativeExecutor().identity()
    assert error.value.code == "executor_unavailable"
    with pytest.raises(EvalError) as error:
        default_judge("claude-code", None).identity()
    assert error.value.code == "judge_unavailable"


@pytest.mark.parametrize(
    ("document", "status"),
    [
        ({"is_error": False, "result": "ok", "total_cost_usd": 0.25}, StopReason.COMPLETED),
        ({"is_error": True, "api_error_status": 429, "result": "x"}, StopReason.RATE_LIMIT),
        ({"is_error": True, "result": "Claude AI usage limit reached|1"}, StopReason.USAGE_CAP),
        ({"is_error": True, "result": "something exploded"}, StopReason.CRASH),
    ],
)
def test_claude_output_is_normalized(document: dict[str, object], status: StopReason) -> None:
    text = "noise\n" + json.dumps(
        {**document, "modelUsage": {"claude-x": {}}, "usage": {"input_tokens": 3}}
    )
    parsed, usage, model, _ = parse_claude_output(text)
    assert parsed == status and model == "claude-x" and usage.input_tokens == 3
    if status == StopReason.COMPLETED:
        assert usage.cost_nusd == 250_000_000
    assert parse_claude_output("not json")[0] == StopReason.CRASH


def test_retrieval_task_persists_namespaced_metrics(
    history_context: ProjectContext, tmp_path: Path
) -> None:
    history = open_history(history_context)
    repo = history.evaluations
    suite = create_suite(
        repo, load_suite(str(small_suite(tmp_path, conditions="[full]", runs=1))), "native", "1"
    )
    lease = repo.acquire_lease(suite.id, {}, owner_gone=lambda owner: True)
    (trial,) = repo.list_trials(suite.id)
    task = HeadlessTask(
        task_id="retrieval-1",
        purpose=TaskPurpose.RETRIEVAL,
        executor="native",
        model=None,
        workspace=tmp_path,
        mission_ref="questions.jsonl",
        gate_mode=GateMode.UNATTENDED,
    )
    metric = MeasurementInput("retrieval.context_precision", "ratio", 0.8, complete=True)
    revision = persist_task_metrics(
        repo, lease, trial.id, task, {"name": "native", "version": "1"}, [metric]
    )
    (result,) = repo.list_results(suite.id, revision)
    assert result.metric == "retrieval.context_precision" and result.mean == 0.8
    assert result.stratum["purpose"] == "retrieval"
    with pytest.raises(EvalError, match="namespace"):
        persist_task_metrics(
            repo,
            lease,
            trial.id,
            task,
            {"name": "native"},
            [MeasurementInput("detection_rate", "ratio", 1.0)],
        )
    with pytest.raises(EvalError):
        HeadlessTask(
            task_id="x",
            purpose=TaskPurpose.RETRIEVAL,
            executor="native",
            model=None,
            workspace=tmp_path,
            mission_ref="m",
            gate_mode=GateMode.UNATTENDED,
            evaluation={"condition": "baseline"},
        )


def test_autonomous_consumer_forces_unattended_skip(history_context: ProjectContext) -> None:
    task = HeadlessTask(
        task_id="auto-1",
        purpose=TaskPurpose.AUTONOMOUS,
        executor="claude-code",
        model=None,
        workspace=None,
        mission_ref="m",
        gate_mode=GateMode.UNATTENDED,
    )
    assert task.gate_mode == GateMode.UNATTENDED
    history = open_history(history_context)
    run_id = history.create_run(history_context, TextMission("Autônomo")).record.id
    payload = {
        "tenant": {"id": "acme"},
        "action": {"operation": "update", "route": "/employees/1/edit"},
        "changes": [{"field": "phone", "new": "2"}],
        "fingerprint": "a" * 64,
    }
    history.request_approval(run_id, "act_1", "a" * 64, payload)
    approve_everything = ApproverSpec(
        rules=(
            ApproverRule(
                id="all",
                tenant="acme",
                operation="update",
                route="/employees/{id}/edit",
                fields=("phone",),
                decision="approve",
            ),
        )
    )
    decisions = TrialDecisions(approve_everything, (), lambda: history, GateMode.UNATTENDED)
    decisions.tick()
    record = history.get_approval("act_1")
    assert record is not None and record.status == ApprovalStatus.SKIPPED
    assert isinstance(decisions.approver, ScriptedApprover)
