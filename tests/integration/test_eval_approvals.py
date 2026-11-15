from collections.abc import Callable
from typing import Any

from lastro.browser.tools import BrowserTools
from lastro.evaluation.decisions import ScriptedApprover, TrialDecisions
from lastro.evaluation.models import ApproverRule, ApproverSpec
from lastro.history import ApprovalStatus, DecidedBy, HistoryStore, RunStatus

from .test_approval_mcp import EMAIL, SAVE, demo_project, run_scenario, writes


def rule(decision: str, fields: tuple[str, ...] = ("email",)) -> ApproverRule:
    return ApproverRule(
        id=f"edit-{decision}",
        tenant="aurora",
        operation="update",
        route="/employees/{id}/edit",
        fields=fields,
        decision=decision,  # type: ignore[arg-type]
        max_uses=2,
    )


def test_scripted_decision_uses_existing_interface_and_is_recorded(
    make_auth_project: Callable[..., Any], demo_server: Any
) -> None:
    async def scenario(runtime: BrowserTools, history: HistoryStore, run_id: str, href: str):
        filled = await runtime.call("fill", EMAIL)
        assert filled["decision"] == "awaiting_approval"
        decisions = TrialDecisions(ApproverSpec(rules=(rule("approve"),)), (), lambda: history)
        decisions.tick()
        recorded = history.approval_decision(filled["approval_id"], run_id)
        assert recorded is not None and recorded.decided_by == DecidedBy.SCRIPTED
        status = await runtime.call(
            "approval_status", {"approval_id": filled["approval_id"], "wait_seconds": 0}
        )
        assert (status["status"], status["decided_by"]) == ("approved", "scripted")

    run_scenario(demo_project(make_auth_project), scenario)


def test_approved_action_is_consumed_once(
    make_auth_project: Callable[..., Any], demo_server: Any
) -> None:
    async def scenario(runtime: BrowserTools, history: HistoryStore, run_id: str, href: str):
        approver = ScriptedApprover(history, ApproverSpec(rules=(rule("approve"),)))
        filled = await runtime.call("fill", EMAIL)
        approver.decide([run_id])
        done = await runtime.call("fill", {**EMAIL, "approval_id": filled["approval_id"]})
        assert (done["status"], done["approval_status"]) == ("ok", "consumed")
        replay = await runtime.call("fill", {**EMAIL, "approval_id": filled["approval_id"]})
        assert replay["approval_status"] == "consumed" and replay["decision"] == "not_executed"
        saved = await runtime.call("submit", SAVE)
        approver.decide([run_id])
        executed = await runtime.call("submit", {**SAVE, "approval_id": saved["approval_id"]})
        assert executed["status"] == "ok", executed
        assert len(writes(demo_server, href)) == 1
        again = await runtime.call("submit", {**SAVE, "approval_id": saved["approval_id"]})
        assert again.get("status") != "ok"
        assert len(writes(demo_server, href)) == 1

    run_scenario(demo_project(make_auth_project), scenario)


def test_refused_gate_has_no_server_request(
    make_auth_project: Callable[..., Any], demo_server: Any
) -> None:
    async def scenario(runtime: BrowserTools, history: HistoryStore, run_id: str, href: str):
        approver = ScriptedApprover(history, ApproverSpec(rules=(rule("skip"),)))
        filled = await runtime.call("fill", EMAIL)
        (decision,) = approver.decide([run_id])
        assert decision.choice.value == "skip" and decision.rule_id == "edit-skip"
        record = history.get_approval(filled["approval_id"], run_id)
        assert record is not None and record.status == ApprovalStatus.SKIPPED
        assert record.decided_by == DecidedBy.SCRIPTED
        retry = await runtime.call("fill", {**EMAIL, "approval_id": filled["approval_id"]})
        assert retry.get("status") != "ok"
        assert writes(demo_server, href) == []
        assert history.get_run(run_id).status == RunStatus.RUNNING

    run_scenario(demo_project(make_auth_project), scenario)


def test_reproduction_gates_use_trial_rules(
    make_auth_project: Callable[..., Any], demo_server: Any
) -> None:
    async def scenario(runtime: BrowserTools, history: HistoryStore, run_id: str, href: str):
        filled = await runtime.call("fill", EMAIL)
        decisions = TrialDecisions(ApproverSpec(rules=(rule("approve"),)), (), lambda: history)
        decisions.tick()
        assert decisions.approver is not None
        assert decisions.approver.uses == {"edit-approve": 1}
        assert {item.id for item in history.list_non_terminal()} >= {run_id}
        record = history.get_approval(filled["approval_id"], run_id)
        assert record is not None and record.decided_by == DecidedBy.SCRIPTED

    run_scenario(demo_project(make_auth_project), scenario)
