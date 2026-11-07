"""Entrada MCP stdio; stdout é reservado ao protocolo."""

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from lastro.approvals.notifications import default_router
from lastro.browser.control import HumanControl
from lastro.browser.tools import (
    KNOWLEDGE_MODELS,
    NO_ACTIVE_RUN,
    NO_RUN,
    BrowserTools,
    catalog,
    input_schema,
)
from lastro.config.loader import ProjectContext, load_project
from lastro.history import HistoryStore, RunRecord, open_history


class PinnedRunError(RuntimeError):
    pass


def pinned_context(
    project: ProjectContext, history: HistoryStore, run_id: str
) -> tuple[ProjectContext, RunRecord]:
    """Run fixada pelo executor nativo: ativa, do mesmo projeto e perfil, com seu executor."""
    run = history.get_active_run(run_id)
    if run is None:
        raise PinnedRunError(f"run {run_id} is not active")
    if run.project_id != project.require_project_id() or run.profile != project.profile_name:
        raise PinnedRunError(f"run {run_id} belongs to another project or profile")
    return project.with_executor(run.executor_name), run


async def serve(
    profile: str | None = None, run_id: str | None = None, config: Path | None = None
) -> None:
    project = load_project(config, profile=profile)
    history = open_history(project)
    if run_id is None:
        run = history.get_active_run()
        # Run do Codex só aceita o servidor fixado pelo launcher, nunca uma sessão avulsa.
        if run is not None and run.executor_name == "codex":
            run = None
    else:
        # A chave do provedor pertence só ao processo do executor, nunca ao browser.
        os.environ.pop("ANTHROPIC_API_KEY", None)
        project, run = pinned_context(project, history, run_id)
    runtime = (
        BrowserTools(project, history, run.id, notifier=default_router(history)) if run else None
    )
    server = Server("lastro")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name=name,
                description="Run-scoped Lastro tool: " + name,
                inputSchema=input_schema(name),
            )
            for name in catalog(runtime is not None and runtime.reproduction)
        ]

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        import json

        if runtime is not None:
            result = await runtime.call(name, arguments)
        elif name in KNOWLEDGE_MODELS:
            result = {"error": NO_ACTIVE_RUN, "message": NO_RUN}
        else:
            result = {"error": NO_RUN}
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    control = HumanControl(runtime) if runtime is not None else None
    if control is not None:
        await control.start()
        assert runtime is not None
        runtime.proofs = control.accept_proof
        # O prazo corre antes da primeira chamada e durante esperas, mesmo sem tools.
        await runtime.monitor.start()
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        if runtime is not None:
            await runtime.monitor.close()
        if control is not None:
            await control.close()
        if runtime is not None:
            runtime.approvals.close()
            await runtime.session.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile")
    parser.add_argument("--run-id")
    parser.add_argument("--config", type=Path)
    arguments = parser.parse_args()
    try:
        asyncio.run(serve(arguments.profile, arguments.run_id, arguments.config))
    except PinnedRunError as error:
        print(f"lastro-browser: {error}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
