from decimal import Decimal
from typing import Any

import pytest

from lastro.autonomous.models import QueueStatus
from lastro.autonomous.runner import effective_project, run_snapshot
from lastro.config.loader import ProjectContext
from lastro.evaluation.decisions import UnattendedDecisions
from lastro.evaluation.executors import RunLaunch
from lastro.evaluation.models import EvalError, GateMode, HeadlessTask, TaskPurpose
from lastro.history import (
    ApprovalStatus,
    ClarificationStatus,
    DecidedBy,
    HistoryStore,
    RunStatus,
    TextMission,
    open_history,
)
from lastro.mission import ClarificationInput, MissionPlanInput

from .autonomous_support import FakeExecutor, coordinator, manual

PLAN: dict[str, Any] = {
    "objectives": ["Validar o telefone do funcionário"],
    "scope": [],
    "criteria": [],
    "gaps": [{"description": "Formato de telefone não informado", "blocking": True}],
    "out_of_scope": [],
    "charter": {
        "target": "employee phone",
        "resources": ["demo application"],
        "information": "validation defects",
        "heuristics": ["boundary_values"],
    },
}


@pytest.fixture
def history(history_context: ProjectContext) -> HistoryStore:
    return open_history(history_context)


def test_headless_task_requires_unattended() -> None:
    with pytest.raises(EvalError) as refused:
        HeadlessTask(
            task_id="aq_x",
            purpose=TaskPurpose.AUTONOMOUS,
            executor="claude-code",
            model=None,
            workspace=None,
            mission_ref="missions/daily.md",
            gate_mode=GateMode.SCRIPTED,
        )
    assert refused.value.code == "invalid_task"


def _approval_script(launch: RunLaunch) -> None:
    history = launch.history
    history.request_approval(
        launch.run_id,
        "act1",
        "a" * 64,
        {"summary": "edit employee #57 (phone)", "fingerprint": "a" * 64},
    )
    decisions = UnattendedDecisions(history)
    decisions.tick()
    history.finish_run(launch.run_id, RunStatus.COMPLETED, None)


def test_require_approval_is_skipped(
    history_context: ProjectContext, history: HistoryStore
) -> None:
    fake = FakeExecutor(script=_approval_script)
    history.autonomous.enqueue(manual())
    (outcome,) = coordinator(history_context, history, fake).drain("homolog")
    assert outcome.run is not None
    (approval,) = history.list_approvals(outcome.run.id)
    assert approval.status == ApprovalStatus.SKIPPED
    assert approval.decided_by == DecidedBy.SCRIPTED
    assert approval.decision_reason is not None
    assert "unattended policy: never approve" in approval.decision_reason
    pending = [item for item in history.pending_items(outcome.run.id) if item.kind == "approval"]
    assert [(item.reference, item.state) for item in pending] == [("act1", "skipped_unattended")]
    assert fake.tasks[0].gate_mode == GateMode.UNATTENDED
    assert fake.tasks[0].purpose == TaskPurpose.AUTONOMOUS


def test_clarification_becomes_gap(history_context: ProjectContext, history: HistoryStore) -> None:
    run_id = history.create_run(history_context, TextMission("Explore o telefone")).record.id
    plan = history.submit_mission_plan(run_id, MissionPlanInput.model_validate(PLAN)).plan
    gap = plan.blocking_gaps[0]
    history.request_clarification(
        run_id,
        ClarificationInput.model_validate(
            {
                "gap_id": gap.id,
                "question": "Qual formato de telefone deve ser aceito?",
                "why_it_matters": "Define os valores de fronteira",
                "options": ["Celular com DDD", "Fixo com DDD"],
            }
        ),
    )
    assert history.get_run(run_id).status == RunStatus.AWAITING_CLARIFICATION
    UnattendedDecisions(history).tick()
    (clarification,) = history.list_clarifications(run_id)
    assert clarification.status == ClarificationStatus.ANSWERED
    assert clarification.answer is not None
    assert clarification.answer["source"] == "system_unavailable"
    assert "Do not assume" in str(clarification.answer["answer"])
    assert history.get_run(run_id).status == RunStatus.RUNNING
    gaps = [item for item in history.pending_items(run_id) if item.kind == "clarification_gap"]
    assert [item.reference for item in gaps] == [gap.id]


def test_duration_and_native_cost_caps(
    history_context: ProjectContext, history: HistoryStore
) -> None:
    item = history.autonomous.enqueue(manual()).item
    project = effective_project(history_context.loaded, item)
    assert project.profile.run_limits.max_minutes == 30
    assert Decimal(project.profile.native.cost_cap_usd) == Decimal("2.00")
    snapshot = run_snapshot(project, item)["autonomous"]
    assert snapshot["gate_mode"] == "unattended"
    assert snapshot["mission_sha256"] == item.mission.sha256
    settings = history_context.config.autonomous.model_copy(
        update={"max_minutes": 10, "native_max_cost_usd": "0.50"}
    )
    config = history_context.config.model_copy(update={"autonomous": settings})
    loaded = history_context.loaded.__class__(history_context.loaded.path, config)
    tighter = effective_project(loaded, item)
    assert tighter.profile.run_limits.max_minutes == 10
    assert tighter.profile.native.cost_cap_usd == "0.50"

    fake = FakeExecutor()
    (outcome,) = coordinator(history_context, history, fake).drain("homolog")
    assert fake.launches[0].max_minutes == 30
    assert outcome.run is not None
    stored = outcome.run.config_snapshot
    assert stored["run_limits"]["max_minutes"] == 30  # type: ignore[index]
    assert stored["autonomous"]["item_id"] == item.id  # type: ignore[index]


@pytest.mark.parametrize(
    ("reachable", "auth", "reason"), [(False, None, "unreachable"), (True, "auth", "auth")]
)
def test_unreachable_and_auth_block_without_retry(
    history_context: ProjectContext,
    history: HistoryStore,
    reachable: bool,
    auth: str | None,
    reason: str,
) -> None:
    fake = FakeExecutor(reachable=reachable, auth=auth)
    history.autonomous.enqueue(manual())
    worker = coordinator(history_context, history, fake)
    (outcome,) = worker.drain("homolog")
    assert outcome.run is not None
    assert (outcome.run.status, outcome.run.status_reason) == (RunStatus.BLOCKED, reason)
    assert outcome.item.status == QueueStatus.BLOCKED
    assert fake.launches == []
    assert outcome.report_path is not None and outcome.report_path.is_file()
    assert worker.drain("homolog") == []
    runs = history.list_runs()
    assert len(runs) == 1
