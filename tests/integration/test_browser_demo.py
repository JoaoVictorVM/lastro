import asyncio
from collections.abc import Callable
from typing import Any, cast

from playwright.async_api import Page

from lastro.browser.tools import BrowserTools
from lastro.history import TextMission, open_history


def test_demo_deactivate_never_reaches_server(
    make_auth_project: Callable[..., Any], demo_server: Any
) -> None:
    project = make_auth_project().context()
    history = open_history(project)
    run = history.create_run(project, TextMission("Explore demo safely"))
    runtime = BrowserTools(project, history, run.record.id)

    async def scenario() -> None:
        try:
            denied = await runtime.call("click", {"target": {"role": "button", "name": "Inativar"}})
            assert denied["reason"] == "no-destructive"
            assert runtime.session.page is None
            result = await runtime.call(
                "navigate", {"url": project.profile.target_url + "/employees"}
            )
            assert result["status"] == "ok", result
            observed = await runtime.call("observe", {})
            assert "<untrusted-content>" in observed["content"]
            page = cast(Page, runtime.session.page)
            assert page is not None
            failed = await page.evaluate(
                "fetch('/employees/1', {method:'DELETE'}).then(()=>false).catch(()=>true)"
            )
            assert failed
            assert runtime.session.network_denial == "denied_method"
            assert not any(
                method == "DELETE" or path.endswith("/deactivate")
                for method, path in demo_server.access_log
            )
        finally:
            await runtime.session.close()

    asyncio.run(scenario())


def test_pre_authorized_update_and_unconfirmed_origin(
    make_auth_project: Callable[..., Any],
) -> None:
    from lastro.browser.demo import FIELDS, demo_policy

    project = make_auth_project().context()
    project.profile.policy = demo_policy()
    project.profile.editable_fields = {"employee": list(FIELDS)}
    history = open_history(project)
    run = history.create_run(project, TextMission("Edit authorized employee"))
    runtime = BrowserTools(project, history, run.record.id)

    async def scenario() -> None:
        try:
            await runtime.call("navigate", {"url": project.profile.target_url + "/employees"})
            page = cast(Page, runtime.session.page)
            href = await page.get_by_role("link", name="Editar", exact=True).first.get_attribute(
                "href"
            )
            assert href is not None
            result = await runtime.call("navigate", {"url": project.profile.target_url + href})
            assert result["status"] == "ok"
            result = await runtime.call(
                "fill", {"target": {"label": "E-mail"}, "value": "qa@example.com"}
            )
            assert result["decision"] == "awaiting_approval", result
            assert history.get_run(run.record.id).status.value == "awaiting_approval"
        finally:
            await runtime.session.close()

    asyncio.run(scenario())


def test_authorized_write_screenshot_and_crash_recovery(
    make_auth_project: Callable[..., Any],
) -> None:
    from lastro.browser.demo import FIELDS, demo_policy
    from lastro.config.policy import Preauthorized

    project = make_auth_project().context()
    project.profile.policy = demo_policy()
    project.profile.policy.preauthorized_records = [
        Preauthorized(tenant="aurora", entity="employee", id=str(i)) for i in range(1, 101)
    ]
    project.profile.editable_fields = {"employee": list(FIELDS)}
    history = open_history(project)
    run = history.create_run(project, TextMission("Edit authorized employee"))
    runtime = BrowserTools(project, history, run.record.id)

    async def scenario() -> None:
        try:
            await runtime.call("navigate", {"url": project.profile.target_url + "/employees"})
            page = cast(Page, runtime.session.page)
            href = await page.get_by_role("link", name="Editar", exact=True).first.get_attribute(
                "href"
            )
            assert href is not None
            await runtime.call("navigate", {"url": project.profile.target_url + href})
            result = await runtime.call(
                "fill", {"target": {"label": "E-mail"}, "value": "qa@example.com"}
            )
            assert result["status"] == "ok", result
            result = await runtime.call("submit", {"target": {"role": "button", "name": "Salvar"}})
            assert result["status"] == "ok", result
            artifact = await runtime.call("screenshot", {})
            assert len(artifact["sha256"]) == 64
            runtime.session.on_crash()
            result = await runtime.call("observe", {})
            assert "Browser restarted" in result["error"]
            assert runtime.session.auth.verified_context is not None
            runtime.session.on_crash()
            result = await runtime.call("observe", {})
            assert history.get_run(run.record.id).status.value == "blocked"
        finally:
            await runtime.session.close()

    asyncio.run(scenario())
