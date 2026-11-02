import asyncio
from typing import Any

from lastro.browser.events import record_call
from lastro.browser.policy import Decision
from lastro.browser.tools import BrowserTools
from lastro.config.loader import ProjectContext
from lastro.history import EventType, HistoryStore, TextMission, open_history

BUNDLE = "b" * 64
PLAN: dict[str, Any] = {
    "objectives": ["Validar o cadastro de funcionários"],
    "scope": ["Criar e editar funcionário"],
    "criteria": [{"text": "O telefone permanece após recarregar", "source": "mission_explicit"}],
    "gaps": [],
    "out_of_scope": ["Excluir funcionários"],
    "charter": {
        "target": "employee registration",
        "resources": ["demo application", "mission criteria"],
        "information": "validation and persistence defects",
        "heuristics": ["boundary_values", "persistence_after_reload"],
    },
}
BLOCKING = [{"description": "Formato de telefone não informado", "blocking": True}]


def runtime_for(
    context: ProjectContext, bundle: str | None = BUNDLE
) -> tuple[HistoryStore, str, BrowserTools]:
    history = open_history(context)
    run_id = history.create_run(
        context, TextMission("Explore o cadastro de funcionários"), skill_bundle_version=bundle
    ).record.id
    return history, run_id, BrowserTools(context, history, run_id)


def call(runtime: BrowserTools, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(runtime.call(name, arguments))


def decisions(history: HistoryStore, run_id: str) -> list[tuple[str | None, str, object]]:
    return [
        (event.tool_name, str(event.policy_decision), event.payload)
        for event in history.list_events(run_id)
        if event.event_type == EventType.POLICY_DECISION
    ]


def test_browser_tools_require_plan_for_skill_run(history_context: ProjectContext) -> None:
    history, run_id, runtime = runtime_for(history_context)

    for name, arguments in (
        ("observe", {}),
        ("navigate", {"url": history_context.profile.target_url + "/employees"}),
        ("click", {"target": {"role": "button", "name": "Salvar"}}),
        ("generate_test_data", {"entity": "employee"}),
        ("report_finding", {}),
    ):
        result = call(runtime, name, arguments)
        assert result["decision"] == "deny", name
        assert result["reason"] == "mission_plan_required", name
    assert runtime.session.page is None and runtime.session.browser is None
    assert all(decision == "deny" for _, decision, _ in decisions(history, run_id))

    history.finish_run(run_id, "completed")
    legacy_history, legacy_run, legacy = runtime_for(history_context, bundle=None)
    assert call(legacy, "generate_test_data", {"entity": "employee"})["status"] == "ok"
    assert legacy_history.get_run(legacy_run).skill_bundle_version is None


def test_metadata_tools_remain_available_before_plan(history_context: ProjectContext) -> None:
    history, run_id, runtime = runtime_for(history_context)

    context = call(runtime, "mission_context", {})
    assert context["plan_status"] == "missing"
    assert context["skill_bundle_sha256"] == BUNDLE
    assert context["source"] == "text"
    assert context["content"].startswith("<mission-content>\nRequirements data")
    assert "Explore o cadastro de funcionários" in context["content"]
    assert context["content"].endswith("</mission-content>")
    unavailable = call(
        runtime,
        "ask_clarification",
        {"gap_id": "gap_x", "question": "?", "why_it_matters": "x", "options": ["a", "b"]},
    )
    assert unavailable["error"] == "invalid_reference"
    check = call(
        runtime,
        "record_test_check",
        {"heuristic": "boundary_values", "outcome": "passed", "action_ids": ["missing"]},
    )
    assert check["error"] == "invalid_reference"
    assert (
        call(runtime, "end_run", {"outcome": "blocked", "reason": "sem plano"})["status"]
        == "blocked"
    )
    tools = [tool for tool, _, _ in decisions(history, run_id)]
    assert tools == ["mission_context", "ask_clarification", "record_test_check", "end_run"]


def test_submit_plan_returns_canonical_charter(history_context: ProjectContext) -> None:
    history, run_id, runtime = runtime_for(history_context)

    result = call(runtime, "submit_mission_plan", PLAN)

    assert result["created"] and result["revision"] == 1
    assert result["exploration_allowed"] and result["blocking_gaps"] == []
    assert result["charter"]["statement"] == (
        "Explore employee registration with demo application, mission criteria "
        "to discover validation and persistence defects"
    )
    assert result["charter"]["timebox_minutes"] == 20
    assert "Charter: Explore employee registration" in result["display"]
    assert "Time box: 20 minutes" in result["display"]
    assert result["criterion_ids"] == [result["criteria"][0]["id"]]
    assert call(runtime, "submit_mission_plan", PLAN)["created"] is False
    assert call(runtime, "mission_context", {})["plan_status"] == "active"
    assert call(runtime, "generate_test_data", {"entity": "employee"})["status"] == "ok"
    over = PLAN | {
        "charter": PLAN["charter"] | {"timebox_minutes": 31},
        "revision_reason": "initial",
    }
    rejected = call(runtime, "submit_mission_plan", over)
    assert rejected["error"] == "invalid_plan"
    assert "exceeds the run limit of 30" in rejected["detail"]
    invalid = call(runtime, "submit_mission_plan", PLAN | {"objectives": []})
    assert "objectives" in invalid["error"]
    assert len(history.list_mission_plans(run_id)) == 1


def test_blocking_gap_requires_clarification(history_context: ProjectContext) -> None:
    history, run_id, runtime = runtime_for(history_context)

    result = call(runtime, "submit_mission_plan", PLAN | {"gaps": BLOCKING})
    gap_id = result["blocking_gaps"][0]

    assert result["exploration_allowed"] is False
    assert "(blocking)" in result["display"]
    denied = call(runtime, "observe", {})
    assert denied["reason"] == "clarification_required"
    asked = call(
        runtime,
        "ask_clarification",
        {
            "gap_id": gap_id,
            "question": "Qual formato de telefone deve ser aceito?",
            "why_it_matters": "Define os valores de fronteira testados",
            "options": ["Celular com DDD", "Fixo com DDD"],
        },
    )
    assert asked["status"] == "unavailable"
    assert asked["reason"] == "clarification_channel_unavailable"
    assert asked["gap_id"] == gap_id
    assert history.list_clarifications(run_id) == []
    assert history.get_run(run_id).status.value == "running"
    assert call(runtime, "observe", {})["reason"] == "clarification_required"
    assert runtime.session.page is None


def allow(history: HistoryStore, run_id: str, action_id: str, tool: str) -> str:
    record_call(history, run_id, action_id, tool, {"step": action_id}, Decision("allow", "", ""))
    return action_id


def test_record_check_requires_real_same_run_actions(history_context: ProjectContext) -> None:
    history, run_id, runtime = runtime_for(history_context)
    criterion = call(runtime, "submit_mission_plan", PLAN)["criterion_ids"][0]
    record_call(history, run_id, "denied", "submit", {}, Decision("deny", "rule", ""))
    allow(history, run_id, "data", "generate_test_data")
    allow(history, run_id, "fill", "fill")

    def check(**changes: Any) -> dict[str, Any]:
        arguments = {
            "heuristic": "boundary_values",
            "outcome": "passed",
            "action_ids": ["fill"],
        } | changes
        return call(runtime, "record_test_check", arguments)

    assert check(action_ids=["denied"])["error"] == "invalid_reference"
    assert check(action_ids=["data"])["detail"].startswith("a check needs at least one browser")
    assert check(criterion_id="criterion_other")["error"] == "invalid_reference"
    accepted = check(action_ids=["data", "fill"], criterion_id=criterion, note="token=abc123456")

    assert accepted["status"] == "recorded"
    (stored,) = history.list_heuristic_checks(run_id)
    assert stored.action_id == accepted["check_id"]
    assert stored.criterion_id == criterion
    assert [action.tool_name for action in stored.actions] == ["generate_test_data", "fill"]
    assert "abc123456" not in stored.note

    history.finish_run(run_id, "completed")
    other_history, other_run, other = runtime_for(history_context)
    call(other, "submit_mission_plan", PLAN)
    foreign = call(
        other,
        "record_test_check",
        {"heuristic": "boundary_values", "outcome": "passed", "action_ids": ["fill"]},
    )
    assert foreign["error"] == "invalid_reference"
    assert other_history.list_heuristic_checks(other_run) == []


def test_record_check_audits_boundary_and_persistence_heuristics(
    history_context: ProjectContext,
) -> None:
    history, run_id, runtime = runtime_for(history_context)
    criterion = call(runtime, "submit_mission_plan", PLAN)["criterion_ids"][0]
    for action, tool in (("fill", "fill"), ("save", "submit"), ("reload", "navigate")):
        allow(history, run_id, action, tool)
    allow(history, run_id, "look", "observe")

    call(
        runtime,
        "record_test_check",
        {"heuristic": "boundary_values", "outcome": "passed", "action_ids": ["fill", "save"]},
    )
    call(
        runtime,
        "record_test_check",
        {
            "heuristic": "persistence_after_reload",
            "criterion_id": criterion,
            "outcome": "failed",
            "action_ids": ["save", "reload", "look"],
            "note": "O telefone desapareceu após recarregar.",
        },
    )

    checks = {check.heuristic: check for check in history.list_heuristic_checks(run_id)}
    assert set(checks) == {"boundary_values", "persistence_after_reload"}
    persistence = checks["persistence_after_reload"]
    assert persistence.outcome == "failed"
    assert [action.action_id for action in persistence.actions] == ["save", "reload", "look"]
    assert all(action.sequence > 0 for action in persistence.actions)
    policy = [p for tool, _, p in decisions(history, run_id) if tool == "record_test_check"]
    assert all(isinstance(payload, dict) and "check" in payload for payload in policy)
