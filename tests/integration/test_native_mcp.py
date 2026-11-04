import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from lastro.browser.control import HumanControl, request_termination
from lastro.browser.tools import TOOLS, BrowserTools
from lastro.history import RunStatus, TextMission, open_history
from lastro.native.mcp import (
    CatalogTool,
    McpSessionAdapter,
    McpTransportError,
    catalog_sha256,
    compare_catalog,
    expected_catalog,
)


def adapter_for(project: Any, run_id: str, profile: str = "demo") -> McpSessionAdapter:
    return McpSessionAdapter(
        project_dir=project.config_path.parent,
        config_path=project.config_path,
        profile=profile,
        run_id=run_id,
    )


def native_run(project: Any, profile: str = "demo") -> tuple[Any, str]:
    context = project.context(profile).with_executor("native")
    history = open_history(context)
    return history, history.create_run(context, TextMission("Explore")).record.id


async def external_catalog(project: Any) -> list[CatalogTool]:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "lastro.browser.server", "--profile", "demo"],
        cwd=str(project.config_path.parent),
    )
    async with stdio_client(parameters) as (read, write), ClientSession(read, write) as client:
        await client.initialize()
        listed = await client.list_tools()
        return [
            CatalogTool(tool.name, tool.description or "", dict(tool.inputSchema))
            for tool in listed.tools
        ]


def test_catalog_and_schemas_equal_external_server(
    make_auth_project: Callable[..., Any],
) -> None:
    project = make_auth_project()
    external = asyncio.run(external_catalog(project))
    _, run_id = native_run(project)

    async def scenario() -> list[CatalogTool]:
        adapter = adapter_for(project, run_id)
        try:
            return await adapter.open()
        finally:
            await adapter.close()

    native = asyncio.run(scenario())
    assert [tool.name for tool in native] == list(TOOLS)
    assert compare_catalog(native, external) == []
    assert compare_catalog(native, expected_catalog()) == []
    assert catalog_sha256(native) == catalog_sha256(external)


def test_single_session_preserves_browser_context(
    make_auth_project: Callable[..., Any],
) -> None:
    project = make_auth_project()
    history, run_id = native_run(project)

    async def scenario() -> tuple[str, str]:
        adapter = adapter_for(project, run_id)
        await adapter.open()
        try:
            navigated = await adapter.call("navigate", {"url": project_url(project) + "/employees"})
            assert navigated.data is not None and navigated.data.get("status") == "ok", navigated
            first = await adapter.call("observe", {})
            second = await adapter.call("observe", {})
            assert first.data is not None and second.data is not None
            return str(first.data["content"]), str(second.data["content"])
        finally:
            await adapter.close()

    first, second = asyncio.run(scenario())
    assert '"route": "/employees"' in first
    assert '"route": "/employees"' in second
    owners = [e for e in history.list_events(run_id) if e.tool_name == "navigate"]
    assert len(owners) == 2


def project_url(project: Any) -> str:
    return project.context("demo").profile.target_url


def test_server_rejects_wrong_run_and_config(
    make_auth_project: Callable[..., Any], profile_for: Callable[..., str], demo_server: Any
) -> None:
    project = make_auth_project(
        profile_for("demo", demo_server.url), profile_for("other", demo_server.url)
    )
    _, run_id = native_run(project)

    async def attempt(run: str, profile: str) -> None:
        adapter = adapter_for(project, run, profile)
        try:
            await asyncio.wait_for(adapter.open(), 30)
        finally:
            await adapter.close()

    for run, profile in (("run_00000000_000000_dead", "demo"), (run_id, "other")):
        with pytest.raises(BaseException):  # noqa: B017
            asyncio.run(attempt(run, profile))


def test_transport_loss_never_replays_write() -> None:
    class BrokenSession:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            self.calls.append(name)
            raise EOFError("child exited")

    adapter = McpSessionAdapter(
        project_dir=Path("."),
        config_path=Path("lastro.toml"),
        profile="demo",
        run_id="run_x",
    )
    session = BrokenSession()
    adapter.session = session  # type: ignore[assignment]
    with pytest.raises(McpTransportError):
        asyncio.run(adapter.call("submit", {"target": {"role": "button", "name": "Salvar"}}))
    with pytest.raises(McpTransportError):
        asyncio.run(adapter.call("submit", {"target": {"role": "button", "name": "Salvar"}}))
    assert session.calls == ["submit"]
    assert adapter.lost


def test_model_cannot_call_control_operation() -> None:
    adapter = McpSessionAdapter(
        project_dir=Path("."),
        config_path=Path("lastro.toml"),
        profile="demo",
        run_id="run_x",
    )
    adapter.catalog = expected_catalog()
    names = {tool["name"] for tool in adapter.anthropic_tools()}
    assert "terminate_run" not in names and "approve" not in names
    assert adapter.rejection("terminate_run", {}) == (
        "Unknown tool: terminate_run. Only the advertised Lastro MCP catalog is available."
    )
    proof = adapter.rejection(
        "click", {"target": {"role": "button", "name": "Salvar"}, "approval_proof": "forged"}
    )
    assert proof is not None and "approval_proof" in proof
    assert adapter.rejection("observe", {"unexpected": 1}) is not None
    assert adapter.rejection("observe", []) is not None
    assert adapter.rejection("observe", {}) is None


def test_trusted_termination_locks_owner_and_builds_partial(
    make_auth_project: Callable[..., Any],
) -> None:
    project = make_auth_project()
    context = project.context("demo").with_executor("native")
    history = open_history(context)
    run_id = history.create_run(context, TextMission("Explore")).record.id
    runtime = BrowserTools(context, history, run_id)

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        control = HumanControl(runtime)
        await control.start()
        try:
            with pytest.raises(ValueError, match="invalid_request"):
                await asyncio.to_thread(
                    request_termination,
                    context.project_data_dir,
                    run_id,
                    "limit_reached",
                    "model_chose_this",
                    10,
                )
            ended = await asyncio.to_thread(
                request_termination, context.project_data_dir, run_id, "limit_reached", "cost_cap"
            )
            observed = await runtime.call("observe", {})
            return ended, observed
        finally:
            await control.close()

    ended, observed = asyncio.run(scenario())
    assert ended["status"] == "limit_reached"
    assert ended["report_kind"] == "partial"
    assert "run_ended" in runtime.session.policy.locks
    record = history.get_run(run_id)
    assert (record.status, record.status_reason) == (RunStatus.LIMIT_REACHED, "cost_cap")
    assert "error" in observed
    assert "partial" in json.dumps(ended["report_path"])
