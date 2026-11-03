import asyncio
import json
from collections.abc import Callable
from typing import Any, cast

from playwright.async_api import Page

from lastro.browser.demo import FIELDS, demo_policy
from lastro.browser.tools import BrowserTools
from lastro.config.loader import ProjectContext
from lastro.history import HistoryStore, RunStatus, TextMission, open_history
from lastro.mission import ClarificationAnswer


def demo_project(make_auth_project: Callable[..., Any]) -> ProjectContext:
    project = make_auth_project().context()
    project.profile.policy = demo_policy()
    project.profile.editable_fields = {"employee": list(FIELDS)}
    return project


def start(project: ProjectContext) -> tuple[HistoryStore, BrowserTools]:
    history = open_history(project)
    run_id = history.create_run(project, TextMission("Explore employees")).record.id
    return history, BrowserTools(project, history, run_id)


def entries(result: dict[str, Any]) -> list[dict[str, Any]]:
    return json.loads(result["content"].split("\n", 1)[1].rsplit("\n", 1)[0])["entries"]


async def close(runtime: BrowserTools) -> None:
    runtime.approvals.close()
    await runtime.session.close()


def test_observation_has_server_route_title_and_run(
    make_auth_project: Callable[..., Any],
) -> None:
    project = demo_project(make_auth_project)
    history, runtime = start(project)

    async def scenario() -> dict[str, Any]:
        try:
            await runtime.call("navigate", {"url": project.profile.target_url + "/employees"})
            return await runtime.call("observe", {})
        finally:
            await close(runtime)

    observed = asyncio.run(scenario())
    record = history.get_observation(observed["observation_id"])

    assert record.run_id == runtime.run_id and record.route == "/employees"
    assert record.screen_title and record.event_id > 0
    assert {"role": "heading", "label": "Funcionários"} in record.landmarks
    assert record.scope.profile == "demo" and record.scope.tenant == "aurora"


def test_landmarks_projection_omits_input_values(make_auth_project: Callable[..., Any]) -> None:
    project = demo_project(make_auth_project)
    history, runtime = start(project)
    base = project.profile.target_url

    async def scenario() -> tuple[dict[str, Any], str]:
        try:
            await runtime.call("navigate", {"url": base + "/employees"})
            page = cast(Page, runtime.session.page)
            href = await page.get_by_role("link", name="Editar", exact=True).first.get_attribute(
                "href"
            )
            assert href is not None
            await runtime.call("navigate", {"url": base + href})
            email = await page.locator("#email").input_value()
            return await runtime.call("observe", {}), email
        finally:
            await close(runtime)

    observed, email = asyncio.run(scenario())
    record = history.get_observation(observed["observation_id"])
    fields = cast(list[dict[str, Any]], record.projection["fields"])
    names = {item["name"] for item in fields}

    assert {"cpf", "email", "phone"} <= names
    assert email and email not in json.dumps(record.projection, ensure_ascii=False)
    assert "value" not in json.dumps(record.projection["fields"])


def test_current_screen_verified_before_memory_reuse(
    make_auth_project: Callable[..., Any],
) -> None:
    project = demo_project(make_auth_project)
    url = project.profile.target_url + "/employees"
    _, runtime = start(project)

    async def first() -> None:
        try:
            await runtime.call("navigate", {"url": url})
            seen = await runtime.call("observe", {})
            proposed = await runtime.call(
                "knowledge_propose",
                {
                    "flow": "employee",
                    "category": "screen_component",
                    "fact": {
                        "type": "screen",
                        "route": "/employees",
                        "landmarks": [{"role": "heading", "label": "Funcionários"}],
                    },
                    "observation_ids": [seen["observation_id"]],
                    "idempotency_key": "employees-screen",
                },
            )
            assert proposed["status"] == "pending", proposed
            await runtime.call("end_run", {"outcome": "completed"})
        finally:
            await close(runtime)

    asyncio.run(first())
    _, runtime = start(project)

    async def second() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        try:
            before = await runtime.call("knowledge_query", {"flow": "employee"})
            (entry,) = entries(before)
            await runtime.call("navigate", {"url": url})
            seen = await runtime.call("observe", {})
            verified = await runtime.call(
                "knowledge_revalidate",
                {
                    "entry_id": entry["id"],
                    "expected_revision": entry["revision"],
                    "observation_id": seen["observation_id"],
                    "idempotency_key": "verify-employees",
                },
            )
            after = await runtime.call("knowledge_query", {"flow": "employee"})
            return before, verified, after
        finally:
            await close(runtime)

    before, verified, after = asyncio.run(second())

    assert entries(before)[0]["requires_revalidation"] is True
    assert verified["status"] == "verified"
    assert entries(after)[0]["requires_revalidation"] is False


def test_memory_cannot_change_policy_decision(make_auth_project: Callable[..., Any]) -> None:
    project = demo_project(make_auth_project)
    base = project.profile.target_url
    history, runtime = start(project)

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            await runtime.call("navigate", {"url": base + "/employees"})
            page = cast(Page, runtime.session.page)
            href = await page.get_by_role("link", name="Editar", exact=True).first.get_attribute(
                "href"
            )
            assert href is not None
            await runtime.call("navigate", {"url": base + href})
            seen = await runtime.call("observe", {})
            remembered = await runtime.call(
                "knowledge_propose",
                {
                    "flow": "employee",
                    "category": "experience",
                    "fact": {
                        "type": "experience",
                        "attempt": "update",
                        "outcome": "success",
                        "context": {"route": href, "observation_id": seen["observation_id"]},
                    },
                    "observation_ids": [seen["observation_id"]],
                    "idempotency_key": "update-ok",
                },
            )
            filled = await runtime.call(
                "fill", {"target": {"label": "Telefone"}, "value": "(11) 91234-0000"}
            )
            return remembered, filled
        finally:
            await close(runtime)

    remembered, filled = asyncio.run(scenario())

    assert remembered["status"] == "pending"
    # Experiência lembrada nunca vira autorização: a escrita continua no gate humano.
    assert filled["decision"] == "awaiting_approval", filled
    assert history.pending_approval(runtime.run_id) is not None


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


class Channel:
    def notify(self, clarification: Any) -> None:
        pass


def test_user_clarification_reused_without_reasking(history_context: ProjectContext) -> None:
    history = open_history(history_context)
    run_id = history.create_run(
        history_context, TextMission("Explore o telefone"), skill_bundle_version="c" * 64
    ).record.id
    runtime = BrowserTools(history_context, history, run_id, clarifications=Channel())
    plan = asyncio.run(runtime.call("submit_mission_plan", PLAN))
    gap_id = plan["blocking_gaps"][0]
    asked = asyncio.run(
        runtime.call(
            "ask_clarification",
            {
                "gap_id": gap_id,
                "question": "Qual formato de telefone deve ser aceito?",
                "why_it_matters": "Define os valores de fronteira",
                "options": ["Celular com DDD", "Fixo com DDD"],
            },
        )
    )
    history.answer_clarification(
        asked["clarification_id"], ClarificationAnswer(answer="Celular com DDD")
    )
    proposed = asyncio.run(
        runtime.call(
            "knowledge_propose",
            {
                "flow": "employee",
                "category": "rule_clarification",
                "fact": {"type": "clarification", "gap_id": gap_id, "subject": "employee"},
                "clarification_id": asked["clarification_id"],
                "idempotency_key": "phone-format",
            },
        )
    )
    asyncio.run(runtime.call("end_run", {"outcome": "completed"}))

    second = history.create_run(
        history_context, TextMission("Explore o telefone"), skill_bundle_version="c" * 64
    ).record.id
    runtime = BrowserTools(history_context, history, second, clarifications=Channel())
    (entry,) = entries(asyncio.run(runtime.call("knowledge_query", {"flow": "employee"})))

    assert proposed["source"] == "user" and proposed["validation_state"] == "user_provided"
    assert entry["validation_state"] == "user_provided" and entry["source"] == "user"
    assert "Celular com DDD" in entry["statement"]
    assert history.list_clarifications(second) == []
    assert history.list_approvals(run_id) == []
    history.finish_run(second, RunStatus.COMPLETED)
