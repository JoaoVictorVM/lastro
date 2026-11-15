from typing import Any

import pytest

from lastro.config.loader import ProjectContext
from lastro.evaluation.decisions import (
    UNAVAILABLE_ANSWER,
    ScriptedApprover,
    ScriptedClarifier,
    rule_matches,
)
from lastro.evaluation.models import (
    ApproverRule,
    ApproverSpec,
    ClarificationSpec,
    EvalError,
    GateMode,
    HeadlessTask,
    TaskPurpose,
)
from lastro.history import ApprovalStatus, DecidedBy, HistoryStore, TextMission, open_history

FINGERPRINT = "a" * 64
RULE = ApproverRule.model_validate_json(
    '{"id": "edit-phone", "tenant": "acme", "operation": "update", '
    '"route": "/employees/{id}/edit", "fields": ["phone"], "decision": "approve", "max_uses": 1}'
)


def payload(**patch: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "tenant": {"id": "acme", "display": "Acme"},
        "action": {"operation": "update", "route": "/employees/7/edit", "method": "POST"},
        "changes": [{"field": "phone", "old": "1", "new": "2"}],
        "fingerprint": FINGERPRINT,
        "summary": "edit employee #1 (phone)",
    }
    return {**base, **patch}


def pending(
    history_context: ProjectContext, data: dict[str, Any] | None = None
) -> tuple[HistoryStore, str, str]:
    history = open_history(history_context)
    run_id = history.create_run(history_context, TextMission("Editar funcionário")).record.id
    record = history.request_approval(run_id, "act_1", FINGERPRINT, data or payload())
    return history, run_id, record.action_id


def test_rule_matches_exact_payload_only() -> None:
    assert rule_matches(RULE, payload())
    assert not rule_matches(RULE, payload(action={"operation": "update", "route": "/employees/7"}))
    assert not rule_matches(
        RULE, payload(action={"operation": "create", "route": "/employees/7/edit"})
    )
    assert not rule_matches(RULE, payload(tenant={"id": "other"}))
    assert not rule_matches(RULE, payload(changes=[{"field": "email", "new": "x"}]))
    two = payload(changes=[{"field": "phone", "new": "2"}, {"field": "email", "new": "x"}])
    assert not rule_matches(RULE, two)


def test_scripted_decision_is_recorded_as_scripted(history_context: ProjectContext) -> None:
    history, run_id, action_id = pending(history_context)
    approver = ScriptedApprover(history, ApproverSpec(rules=(RULE,)))
    (decision,) = approver.decide([run_id])
    assert decision.rule_id == "edit-phone" and decision.choice.value == "approve"
    record = history.get_approval(action_id)
    assert record is not None and record.decided_by == DecidedBy.SCRIPTED
    assert record.status == ApprovalStatus.APPROVED
    assert "edit-phone" in (record.decision_reason or "")


def test_unknown_gate_is_skipped(history_context: ProjectContext) -> None:
    history, run_id, action_id = pending(
        history_context, payload(changes=[{"field": "email", "old": "a", "new": "b"}])
    )
    (decision,) = ScriptedApprover(history, ApproverSpec(rules=(RULE,))).decide([run_id])
    assert decision.choice.value == "skip" and decision.rule_id is None
    record = history.get_approval(action_id)
    assert record is not None and record.decided_by == DecidedBy.SCRIPTED


def test_changed_fingerprint_is_not_approved(history_context: ProjectContext) -> None:
    history, run_id, _ = pending(history_context, payload(fingerprint="b" * 64))
    (decision,) = ScriptedApprover(history, ApproverSpec(rules=(RULE,))).decide([run_id])
    assert decision.choice.value == "skip"
    assert "fingerprint" in decision.reason


def test_unattended_cannot_approve(history_context: ProjectContext) -> None:
    history, run_id, _ = pending(history_context)
    approver = ScriptedApprover(history, ApproverSpec(rules=(RULE,)), GateMode.UNATTENDED)
    (decision,) = approver.decide([run_id])
    assert decision.choice.value == "skip"
    with pytest.raises(EvalError):
        HeadlessTask(
            task_id="t",
            purpose=TaskPurpose.AUTONOMOUS,
            executor="native",
            model=None,
            workspace=None,
            mission_ref="m",
            gate_mode=GateMode.SCRIPTED,
        )


def test_rule_uses_are_limited(history_context: ProjectContext) -> None:
    history, run_id, _ = pending(history_context)
    approver = ScriptedApprover(history, ApproverSpec(rules=(RULE,)))
    approver.uses["edit-phone"] = 1
    (decision,) = approver.decide([run_id])
    assert decision.choice.value == "skip" and decision.reason == "rule uses exhausted"


def test_missing_clarification_blocks_without_inventing_answer(
    history_context: ProjectContext,
) -> None:
    import asyncio

    from lastro.browser.tools import BrowserTools
    from lastro.history.models import ClarificationStatus

    history = open_history(history_context)
    run_id = history.create_run(
        history_context, TextMission("Regra de férias"), skill_bundle_version="c" * 64
    ).record.id
    runtime = BrowserTools(history_context, history, run_id, clarifications=SilentChannel())
    plan = {
        "objectives": ["Verificar a regra de férias coletivas"],
        "scope": [],
        "criteria": [],
        "gaps": [{"description": "Período de férias coletivas não informado", "blocking": True}],
        "out_of_scope": [],
        "charter": {
            "target": "employee registration",
            "resources": ["demo application"],
            "information": "vacation rule",
            "heuristics": ["boundary_values"],
        },
    }
    accepted = asyncio.run(runtime.call("submit_mission_plan", plan))
    gap_id = accepted["blocking_gaps"][0]
    asked = asyncio.run(
        runtime.call(
            "ask_clarification",
            {
                "gap_id": gap_id,
                "question": "Qual é o vacation period da clínica?",
                "why_it_matters": "Define a regra",
                "options": ["Janeiro", "Julho"],
            },
        )
    )
    assert asked["status"] == "pending"
    fixtures = (ClarificationSpec(gap_key="vacation-period", answer=None),)
    clarifier = ScriptedClarifier(history, fixtures)
    assert clarifier.answer([run_id]) == 1
    (item,) = history.list_clarifications(run_id)
    assert item.status == ClarificationStatus.ANSWERED
    assert item.answer is not None and item.answer.get("answer") == UNAVAILABLE_ANSWER
    assert clarifier.answered == [(run_id, gap_key_of(gap_id), False)]


class SilentChannel:
    def notify(self, clarification: object) -> None:
        pass


def gap_key_of(value: str) -> str:
    from lastro.evaluation.decisions import gap_key

    return gap_key(value)
