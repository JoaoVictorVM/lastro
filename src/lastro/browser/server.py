"""Entrada MCP stdio; stdout é reservado ao protocolo."""

import argparse
import asyncio
from typing import Any

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from lastro.browser.control import HumanControl
from lastro.browser.tools import NO_RUN, TOOLS, Arguments, BrowserTools
from lastro.config.loader import load_project
from lastro.history import open_history
from lastro.testdata import TestDataRequest


async def serve(profile: str | None = None) -> None:
    project = load_project(profile=profile)
    history = open_history(project)
    run = history.get_active_run()
    runtime = BrowserTools(project, history, run.id) if run else None
    server = Server("lastro")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name=name,
                description="Run-scoped Lastro tool: " + name,
                inputSchema=TestDataRequest.model_json_schema()
                if name == "generate_test_data"
                else Arguments.model_json_schema(),
            )
            for name in TOOLS
        ]

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        import json

        result = await runtime.call(name, arguments) if runtime else {"error": NO_RUN}
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    control = HumanControl(runtime) if runtime is not None else None
    if control is not None:
        await control.start()
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        if control is not None:
            await control.close()
        if runtime is not None:
            await runtime.session.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile")
    arguments = parser.parse_args()
    asyncio.run(serve(arguments.profile))


if __name__ == "__main__":
    main()
