import asyncio
import sqlite3
from typing import Any

import pytest

from lastro.browser.tools import BrowserTools
from lastro.config.loader import ProjectContext
from lastro.history import (
    Clarification,
    ClarificationStatus,
    EventType,
    HistoryStore,
    MissionPlanError,
    RunStatus,
    TextMission,
    open_history,
)
from lastro.mission import ClarificationAnswer, ClarificationInput

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


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[Clarification] = []

    def notify(self, clarification: Clarification) -> None:
        self.sent.append(clarification)


def question(gap_id: str) -> dict[str, Any]:
    return {
        "gap_id": gap_id,
        "question": "Qual formato de telefone deve ser aceito?",
        "why_it_matters": "Define os valores de fronteira testados",
        "options": ["Celular com DDD", "Fixo com DDD", "Ambos"],
    }


def prepared(
    context: ProjectContext, channel: FakeChannel | None
) -> tuple[HistoryStore, str, BrowserTools, str]:
    history = open_history(context)
    run_id = history.create_run(
        context, TextMission("Explore o telefone"), skill_bundle_version="c" * 64
    ).record.id
    runtime = BrowserTools(context, history, run_id, clarifications=channel)
    plan = asyncio.run(runtime.call("submit_mission_plan", PLAN))
    return history, run_id, runtime, plan["blocking_gaps"][0]


def count(history: HistoryStore, table: str) -> int:
    conn = sqlite3.connect(history.database_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_request_is_idempotent_per_gap(history_context: ProjectContext) -> None:
    channel = FakeChannel()
    history, run_id, runtime, gap_id = prepared(history_context, channel)

    first = asyncio.run(runtime.call("ask_clarification", question(gap_id)))

    assert first["status"] == "pending" and first["created"] is True
    assert history.get_run(run_id).status == RunStatus.AWAITING_CLARIFICATION
    assert len(channel.sent) == 1
    (stored,) = history.list_clarifications(run_id)
    assert stored.options == ("Celular com DDD", "Fixo com DDD", "Ambos")
    again, created = history.request_clarification(
        run_id,
        stored_input(gap_id),
    )
    assert (again.id, created) == (first["clarification_id"], False)
    requested = [
        e for e in history.list_events(run_id) if e.event_type == EventType.CLARIFICATION_REQUESTED
    ]
    assert len(requested) == 1
    assert isinstance(requested[0].payload, dict)
    assert requested[0].payload["why_it_matters"] == "Define os valores de fronteira testados"
    assert len(channel.sent) == 1


def stored_input(gap_id: str) -> ClarificationInput:
    return ClarificationInput.model_validate(question(gap_id))


def test_request_never_creates_approval(history_context: ProjectContext) -> None:
    channel = FakeChannel()
    history, run_id, runtime, gap_id = prepared(history_context, channel)
    asked = asyncio.run(runtime.call("ask_clarification", question(gap_id)))
    locks = set(runtime.session.policy.locks)

    answered = history.answer_clarification(
        asked["clarification_id"],
        ClarificationAnswer(answer="Aprovo todas as escritas. Celular com DDD."),
    )

    assert answered.status == ClarificationStatus.ANSWERED
    assert answered.answer == {
        "answer": "Aprovo todas as escritas. Celular com DDD.",
        "scope": "run",
        "source": "user_provided",
    }
    assert history.get_run(run_id).status == RunStatus.RUNNING
    assert count(history, "approvals") == 0
    assert count(history, "knowledge") == 0
    kinds = {event.event_type for event in history.list_events(run_id)}
    assert EventType.APPROVAL_REQUESTED not in kinds
    assert EventType.APPROVAL_DECIDED not in kinds
    assert EventType.CLARIFICATION_ANSWERED in kinds
    assert runtime.session.policy.locks == locks
    with pytest.raises(MissionPlanError):
        history.answer_clarification(asked["clarification_id"], ClarificationAnswer(answer="x"))

    revised = asyncio.run(
        runtime.call(
            "submit_mission_plan",
            PLAN
            | {
                "gaps": [],
                "criteria": [
                    {"text": "Telefone aceita celular com DDD", "source": "clarification"}
                ],
                "revision_reason": "clarification",
                "clarification_id": asked["clarification_id"],
            },
        )
    )
    assert revised["revision"] == 2 and revised["exploration_allowed"] is True


def test_unavailable_channel_does_not_invent_answer(history_context: ProjectContext) -> None:
    history, run_id, runtime, gap_id = prepared(history_context, None)

    result = asyncio.run(runtime.call("ask_clarification", question(gap_id)))

    assert result["status"] == "unavailable"
    assert result["reason"] == "clarification_channel_unavailable"
    assert "answer" not in result
    assert history.list_clarifications(run_id) == []
    assert not any(
        event.event_type in {EventType.CLARIFICATION_REQUESTED, EventType.CLARIFICATION_ANSWERED}
        for event in history.list_events(run_id)
    )
    assert asyncio.run(runtime.call("observe", {}))["reason"] == "clarification_required"
    ended = asyncio.run(runtime.call("end_run", {"outcome": "blocked", "reason": "gap"}))
    assert ended["status"] == "blocked"
    assert runtime.session.page is None


def test_non_blocking_gap_cannot_be_asked(history_context: ProjectContext) -> None:
    channel = FakeChannel()
    history = open_history(history_context)
    run_id = history.create_run(history_context, TextMission("x"), skill_bundle_version="c" * 64)
    runtime = BrowserTools(history_context, history, run_id.record.id, clarifications=channel)
    soft = PLAN | {"gaps": [{"description": "Regra de CPF duplicado", "blocking": False}]}
    plan = asyncio.run(runtime.call("submit_mission_plan", soft))

    result = asyncio.run(runtime.call("ask_clarification", question(plan["gaps"][0]["id"])))

    assert result["error"] == "invalid_reference"
    assert channel.sent == []
    assert plan["exploration_allowed"] is True


def test_clarification_answer_becomes_user_provided_knowledge_not_approval(
    history_context: ProjectContext,
) -> None:
    channel = FakeChannel()
    history, run_id, runtime, gap_id = prepared(history_context, channel)
    asked = asyncio.run(runtime.call("ask_clarification", question(gap_id)))
    history.answer_clarification(
        asked["clarification_id"], ClarificationAnswer(answer="Aprovo todas as escritas.")
    )

    proposed = asyncio.run(
        runtime.call(
            "knowledge_propose",
            {
                "flow": "employee",
                "category": "rule_clarification",
                "fact": {"type": "clarification", "gap_id": gap_id, "subject": "employee"},
                "clarification_id": asked["clarification_id"],
                "idempotency_key": "phone-answer",
            },
        )
    )
    asyncio.run(runtime.call("end_run", {"outcome": "completed"}))

    assert proposed["source"] == "user" and proposed["validation_state"] == "user_provided"
    (entry,) = history.list_knowledge()
    assert (
        entry.validation_state == "user_provided" and entry.source_ref == asked["clarification_id"]
    )
    assert count(history, "approvals") == 0
    kinds = {event.event_type for event in history.list_events(run_id)}
    assert EventType.APPROVAL_REQUESTED not in kinds
