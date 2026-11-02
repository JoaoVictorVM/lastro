import asyncio
from collections.abc import Callable
from typing import Any, cast

import pytest
from playwright.async_api import Page

from lastro.browser.demo import FIELDS, demo_policy
from lastro.browser.tools import BrowserTools
from lastro.config.policy import Preauthorized
from lastro.history import EventType, PolicyDecision, TextMission, open_history
from lastro_demo.flags import FlagState

MISSION = (
    "Explore a edição de funcionários. O telefone salvo deve permanecer após recarregar a página. "
    "Telefones aceitam 10 ou 11 dígitos."
)
PLAN: dict[str, Any] = {
    "objectives": ["Validar a edição do telefone de funcionários"],
    "scope": ["Editar telefone de funcionário existente"],
    "criteria": [
        {
            "text": "O telefone salvo permanece após recarregar a página",
            "source": "mission_explicit",
        },
        {"text": "Telefones aceitam 10 ou 11 dígitos", "source": "mission_explicit"},
    ],
    "gaps": [],
    "out_of_scope": ["Criar e inativar funcionários"],
    "charter": {
        "target": "employee phone editing",
        "resources": ["demo application", "mission criteria"],
        "information": "validation and persistence defects",
        "heuristics": ["boundary_values", "persistence_after_reload"],
    },
}


@pytest.fixture
def demo_flags() -> FlagState:
    return FlagState.only("d02")


def test_demo_mission_records_boundary_and_persistence_checks(
    make_auth_project: Callable[..., Any],
) -> None:
    project = make_auth_project().context()
    project.profile.policy = demo_policy()
    project.profile.policy.preauthorized_records = [
        Preauthorized(tenant="aurora", entity="employee", id=str(i)) for i in range(1, 101)
    ]
    project.profile.editable_fields = {"employee": list(FIELDS)}
    history = open_history(project)
    run_id = history.create_run(
        project, TextMission(MISSION), skill_bundle_version="d" * 64
    ).record.id
    runtime = BrowserTools(project, history, run_id)
    base = project.profile.target_url

    async def scenario() -> dict[str, Any]:
        try:
            early = await runtime.call("navigate", {"url": base + "/employees"})
            assert early["reason"] == "mission_plan_required"
            context = await runtime.call("mission_context", {})
            assert "permanecer após recarregar" in context["content"]
            plan = await runtime.call("submit_mission_plan", PLAN)
            assert plan["exploration_allowed"], plan
            await runtime.call("navigate", {"url": base + "/employees"})
            page = cast(Page, runtime.session.page)
            href = await page.get_by_role("link", name="Editar", exact=True).first.get_attribute(
                "href"
            )
            assert href is not None
            edit = await runtime.call("navigate", {"url": base + href})
            phone = {"label": "Telefone"}
            save = {"role": "button", "name": "Salvar"}
            below = await runtime.call("fill", {"target": phone, "value": "(11) 3456-789"})
            rejected = await runtime.call("submit", {"target": save})
            seen_below = await runtime.call("observe", {})
            assert "telefone" in seen_below["content"].lower()
            await runtime.call("navigate", {"url": base + href})
            minimum = await runtime.call("fill", {"target": phone, "value": "(11) 3456-7890"})
            saved = await runtime.call("submit", {"target": save})
            reload = await runtime.call("navigate", {"url": base + href})
            seen_after = await runtime.call("observe", {})
            persisted = "(11) 3456-7890" in seen_after["content"]
            boundary = await runtime.call(
                "record_test_check",
                {
                    "heuristic": "boundary_values",
                    "criterion_id": plan["criterion_ids"][1],
                    "outcome": "passed",
                    "action_ids": [
                        below["action_id"],
                        rejected["action_id"],
                        seen_below["action_id"],
                        minimum["action_id"],
                        saved["action_id"],
                    ],
                    "note": "9 dígitos rejeitado; 10 dígitos aceito no envio.",
                },
            )
            persistence = await runtime.call(
                "record_test_check",
                {
                    "heuristic": "persistence_after_reload",
                    "criterion_id": plan["criterion_ids"][0],
                    "outcome": "passed" if persisted else "failed",
                    "action_ids": [
                        edit["action_id"],
                        saved["action_id"],
                        reload["action_id"],
                        seen_after["action_id"],
                    ],
                    "note": "Telefone conferido após recarregar a página de edição.",
                },
            )
            return {"boundary": boundary, "persistence": persistence, "persisted": persisted}
        finally:
            await runtime.session.close()

    result = asyncio.run(scenario())

    assert result["boundary"].get("status") == "recorded", result
    assert result["persistence"].get("status") == "recorded", result
    assert result["persisted"] is False
    checks = {check.heuristic: check for check in history.list_heuristic_checks(run_id)}
    assert set(checks) == {"boundary_values", "persistence_after_reload"}
    assert checks["persistence_after_reload"].outcome == "failed"
    allowed = {
        event.correlation_id
        for event in history.list_events(run_id)
        if event.event_type == EventType.POLICY_DECISION
        and event.policy_decision == PolicyDecision.ALLOW
    }
    for check in checks.values():
        assert {action.action_id for action in check.actions} <= allowed
        assert any(action.tool_name in {"fill", "submit", "navigate"} for action in check.actions)
