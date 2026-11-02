import asyncio
from collections.abc import Callable
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import Page

from lastro.browser.bindings import ExpectedRequest
from lastro.browser.events import record_call
from lastro.browser.policy import Decision
from lastro.browser.session import BrowserSession
from lastro.browser.tools import BrowserTools
from lastro.config.loader import ProjectContext
from lastro.config.policy import Binding, RequestBinding, Target
from lastro.config.testdata import EntityTestData, TestDataSettings
from lastro.history import (
    CleanupStatus,
    EventType,
    HistoryStore,
    RunStatus,
    TextMission,
    open_history,
)


def binding(**request: Any) -> Binding:
    return Binding(
        id="create",
        entity="employee",
        operation="create",
        route="/new",
        target=Target(role="button", name="save"),
        request=RequestBinding(method="POST", path="/api/employees", **request),
    )


def deliver(
    context: ProjectContext,
    contract: Binding,
    *,
    status: int = 201,
    body: object = None,
    headers: dict[str, str] | None = None,
    times: int = 1,
    method: str = "POST",
) -> tuple[HistoryStore, str, BrowserSession]:
    history = open_history(context)
    run_id = history.create_run(context, TextMission("create")).record.id
    context.profile.test_data = TestDataSettings(
        entities={"employee": EntityTestData(detail_path="/employees/{id}")}
    )
    session = BrowserSession(context, history, run_id, lambda: None)
    record_call(history, run_id, "create-action", "submit", {}, Decision("allow", "", ""))
    url = context.profile.target_url + "/api/employees"
    expected = ExpectedRequest(contract, url, {}, "create-action", used=True)

    async def scenario() -> None:
        for _ in range(times):
            request = Mock()
            request.is_navigation_request.return_value = False
            request.method = method
            response = Mock(request=request, status=status, url=url, headers=headers or {})
            response.json = (
                AsyncMock(side_effect=ValueError("not json"))
                if body is None
                else AsyncMock(return_value=body)
            )
            session.request_actions[request] = expected
            session.response(response)
            await asyncio.gather(*tuple(session.response_tasks))

    asyncio.run(scenario())
    assert expected.completed.is_set()
    return history, run_id, session


def test_location_identifier_is_same_origin(history_context: ProjectContext) -> None:
    contract = binding(location_id=True)
    history, run_id, session = deliver(
        history_context, contract, headers={"location": "/api/employees/41"}
    )
    (record,) = history.list_created_records(run_id=run_id)
    assert record.system_identifier == "41"
    assert record.detail_path == "/employees/41"
    assert session.provenance.origin("employee", "41") == "created_in_run"

    history.finish_run(run_id, "completed")
    history, run_id, session = deliver(
        history_context, contract, headers={"location": "https://evil.example/employees/41"}
    )
    (record,) = history.list_created_records(run_id=run_id)
    assert record.system_identifier == "unknown"
    assert session.provenance.origin("employee", "41") == "unknown"


def test_missing_identifier_persists_unknown_without_provenance(
    history_context: ProjectContext,
) -> None:
    history, run_id, session = deliver(
        history_context, binding(response_id="data.id"), body={"data": {"id": True}}
    )

    (record,) = history.list_created_records(run_id=run_id)
    assert record.system_identifier == "unknown"
    assert record.warnings == ("identifier_not_extracted",)
    assert record.cleanup_status == CleanupStatus.PENDING
    assert record.detail_path is None
    assert session.provenance.origin("employee", "unknown") == "unknown"
    assert "history" not in session.policy.locks


def test_duplicate_response_delivery_is_idempotent(history_context: ProjectContext) -> None:
    history, run_id, _ = deliver(
        history_context, binding(response_id="id"), body={"id": 7}, times=2
    )

    assert [r.system_identifier for r in history.list_created_records(run_id=run_id)] == ["7"]
    created = [e for e in history.list_events(run_id) if e.event_type == EventType.RECORD_CREATED]
    assert len(created) == 1


def test_unexpected_2xx_is_tracked_with_warning(history_context: ProjectContext) -> None:
    history, run_id, _ = deliver(
        history_context, binding(response_id="id"), status=200, body={"id": "9"}
    )

    (record,) = history.list_created_records(run_id=run_id)
    assert record.unexpected_success_status == 200
    assert record.warnings == ("unexpected_success_status",)


def test_non_2xx_is_not_created_record(history_context: ProjectContext) -> None:
    for status, method in ((302, "POST"), (422, "POST"), (500, "POST"), (201, "PUT")):
        history, run_id, _ = deliver(
            history_context,
            binding(response_id="id"),
            status=status,
            body={"id": "9"},
            method=method,
        )
        assert history.list_created_records(run_id=run_id) == []
        history.finish_run(run_id, "completed")


def test_response_tenant_mismatch_records_then_blocks_writes(
    history_context: ProjectContext,
) -> None:
    history, run_id, session = deliver(
        history_context,
        binding(response_id="id", response_tenant="tenant.id"),
        body={"id": "5", "tenant": {"id": "boreal"}},
    )

    (record,) = history.list_created_records(run_id=run_id)
    assert record.tenant == "boreal"
    assert record.expected_tenant == "acme"
    assert "tenant_mismatch" in record.warnings
    assert "tenant" in session.policy.locks
    assert session.provenance.origin("employee", "5") == "unknown"
    message = (
        "Record created in unexpected tenant 'boreal'. "
        "Writes stopped; verify and clean up manually."
    )
    assert session.tenant_mismatch == message
    run = history.get_run(run_id)
    assert run.status == RunStatus.BLOCKED
    assert run.status_reason == message


def test_demo_create_persists_response_identifier(
    make_auth_project: Callable[..., Any], demo_server: Any
) -> None:
    from lastro.browser.demo import FIELDS, demo_policy, demo_test_data

    project = make_auth_project().context()
    project.profile.policy = demo_policy()
    project.profile.test_data = demo_test_data()
    project.profile.editable_fields = {"employee": list(FIELDS)}
    history = open_history(project)
    run = history.create_run(project, TextMission("Create employees"))
    runtime = BrowserTools(project, history, run.record.id)
    base = project.profile.target_url

    async def create_one() -> str:
        data = await runtime.call("generate_test_data", {"entity": "employee"})
        values = data["values"]
        assert (await runtime.call("navigate", {"url": base + "/employees/new"}))["status"] == "ok"
        for label, value in (
            ("Nome completo", values["name"]),
            ("CPF", values["cpf"]),
            ("E-mail", values["email"]),
            ("Telefone", values["phone"]),
            ("Data de admissão", values["hire_date"]),
        ):
            result = await runtime.call("fill", {"target": {"label": label}, "value": value})
            assert result == {"status": "ok"}, result
        result = await runtime.call("select", {"target": {"label": "Perfil"}, "value": "medico"})
        assert result == {"status": "ok"}, result
        result = await runtime.call("submit", {"target": {"role": "button", "name": "Salvar"}})
        assert result == {"status": "ok"}, result
        return values["name"]

    async def scenario() -> None:
        try:
            names = [await create_one(), await create_one()]
            records = history.list_created_records(run_id=run.record.id)
            assert len(records) == 2
            for record, name in zip(records, names, strict=True):
                assert record.system_identifier.isdigit()
                assert record.tenant == "aurora"
                assert record.detail_path == f"/employees/{record.system_identifier}"
                assert record.cleanup_instructions
                await runtime.call("navigate", {"url": base + record.detail_path})
                page = cast(Page, runtime.session.page)
                assert name in await page.content()
            created = records[0].system_identifier
            assert runtime.provenance.origin("employee", created) == "created_in_run"
            await runtime.call("navigate", {"url": f"{base}/employees/{created}/edit"})
            result = await runtime.call(
                "fill", {"target": {"label": "Telefone"}, "value": "(11) 91234-5678"}
            )
            assert result == {"status": "ok"}, result
            assert not any(method == "DELETE" for method, _ in demo_server.access_log)
        finally:
            await runtime.session.close()

    asyncio.run(scenario())


def test_f03_f06_created_record_uses_run_and_captured_response(
    make_auth_project: Callable[..., Any],
) -> None:
    from playwright.async_api import Response

    from lastro.browser.demo import FIELDS, demo_policy, demo_test_data

    project = make_auth_project().context()
    project.profile.policy = demo_policy()
    project.profile.test_data = demo_test_data()
    project.profile.editable_fields = {"employee": list(FIELDS)}
    history = open_history(project)
    run = history.create_run(project, TextMission("Create one employee"))
    runtime = BrowserTools(project, history, run.record.id)
    base = project.profile.target_url
    locations: list[str] = []

    def capture(response: Response) -> None:
        if response.request.method == "POST":
            locations.append(response.headers.get("location", ""))

    async def scenario() -> None:
        try:
            values = (await runtime.call("generate_test_data", {"entity": "employee"}))["values"]
            await runtime.call("navigate", {"url": base + "/employees/new"})
            assert runtime.session.context is not None
            runtime.session.context.on("response", capture)
            for label, key in (
                ("Nome completo", "name"),
                ("CPF", "cpf"),
                ("E-mail", "email"),
                ("Data de admissão", "hire_date"),
            ):
                await runtime.call("fill", {"target": {"label": label}, "value": values[key]})
            await runtime.call("select", {"target": {"label": "Perfil"}, "value": "medico"})
            result = await runtime.call("submit", {"target": {"role": "button", "name": "Salvar"}})
            assert result == {"status": "ok"}, result
        finally:
            await runtime.session.close()

    asyncio.run(scenario())
    (record,) = history.list_created_records(run_id=run.record.id)
    assert record.run_id == run.record.id
    assert locations == [f"/employees/{record.system_identifier}"]
    submit = next(
        event
        for event in history.list_events(run.record.id)
        if event.event_type == EventType.TOOL_CALL and event.tool_name == "submit"
    )
    assert record.action_id == submit.correlation_id


@pytest.mark.skip(reason="integração de relatório pendente até existir o report builder")
def test_report_created_records_lists_every_record_with_identifier() -> None:
    pass
