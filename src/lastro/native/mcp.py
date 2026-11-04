"""Sessão MCP persistente com o servidor do Lastro e tradução genérica do catálogo.

Nenhuma ferramenta é implementada aqui: nome, descrição e schema vêm de `list_tools`, e toda
execução passa por `call_tool` no mesmo subprocesso fixado à run.
"""

import asyncio
import contextlib
import json
import os
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO, cast

import jsonschema
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import PaginatedRequestParams, TextContent

from lastro.history.redaction import canonical_json, sha256_hex

PROVIDER_ENV_PREFIX = "ANTHROPIC_"
CONTROL_ARGUMENTS = frozenset({"approval_proof"})
CHILD_EXIT_TIMEOUT_SECONDS = 10.0


class McpTransportError(RuntimeError):
    """Sessão perdida: o resultado da ferramenta é desconhecido e nada é repetido."""


class CatalogMismatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class CatalogTool:
    name: str
    description: str
    input_schema: dict[str, Any]

    def document(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass(frozen=True)
class ToolOutcome:
    text: str
    data: dict[str, Any] | None
    is_error: bool


def expected_catalog() -> list[CatalogTool]:
    """Mesmo catálogo anunciado aos executores externos, sem aliases nem tools novas."""
    from lastro.browser.tools import TOOLS, input_schema

    return [
        CatalogTool(name, "Run-scoped Lastro tool: " + name, input_schema(name)) for name in TOOLS
    ]


def catalog_sha256(tools: list[CatalogTool]) -> str:
    ordered = sorted(tools, key=lambda tool: tool.name)
    return sha256_hex(
        canonical_json([{"name": tool.name, "input_schema": tool.input_schema} for tool in ordered])
    )


def compare_catalog(found: list[CatalogTool], expected: list[CatalogTool]) -> list[str]:
    problems: list[str] = []
    found_by_name = {tool.name: tool for tool in found}
    expected_by_name = {tool.name: tool for tool in expected}
    if len(found_by_name) != len(found):
        problems.append("duplicate tool names")
    for name in sorted(set(found_by_name) ^ set(expected_by_name)):
        problems.append(f"unexpected or missing tool: {name}")
    for name in sorted(set(found_by_name) & set(expected_by_name)):
        if found_by_name[name].input_schema != expected_by_name[name].input_schema:
            problems.append(f"schema differs: {name}")
    return problems


def child_environment() -> dict[str, str]:
    """Ambiente do subprocesso sem a chave nem outras variáveis do provedor."""
    return {
        key: value for key, value in os.environ.items() if not key.startswith(PROVIDER_ENV_PREFIX)
    }


class McpSessionAdapter:
    def __init__(
        self,
        *,
        project_dir: Path,
        config_path: Path,
        profile: str,
        run_id: str,
        errlog: TextIO | None = None,
    ) -> None:
        self.project_dir = project_dir
        self.config_path = config_path
        self.profile = profile
        self.run_id = run_id
        self.errlog = errlog
        self.session: ClientSession | None = None
        self.owner: asyncio.Task[None] | None = None
        self.ready = asyncio.Event()
        self.closing = asyncio.Event()
        self.failure: BaseException | None = None
        self.catalog: list[CatalogTool] = []
        self.lost = False

    def parameters(self) -> StdioServerParameters:
        return StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "lastro.browser.server",
                "--profile",
                self.profile,
                "--config",
                str(self.config_path.resolve()),
                "--run-id",
                self.run_id,
            ],
            cwd=str(self.project_dir),
            env=child_environment(),
        )

    async def open(self) -> list[CatalogTool]:
        """Sobe o filho numa task dona: o contexto do transporte abre e fecha na mesma task."""
        self.owner = asyncio.create_task(self.serve())
        await self.ready.wait()
        session = self.session
        if session is None:
            raise McpTransportError("MCP server did not start") from self.failure
        tools: list[CatalogTool] = []
        cursor: str | None = None
        while True:
            page = await session.list_tools(
                params=PaginatedRequestParams(cursor=cursor) if cursor else None
            )
            tools += [
                CatalogTool(tool.name, tool.description or "", dict(tool.inputSchema))
                for tool in page.tools
            ]
            cursor = page.nextCursor
            if not cursor:
                break
        self.catalog = tools
        return tools

    async def serve(self) -> None:
        errlog = self.errlog if self.errlog is not None else sys.stderr
        try:
            async with AsyncExitStack() as stack:
                read, write = await stack.enter_async_context(
                    stdio_client(self.parameters(), errlog)
                )
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self.session = session
                self.ready.set()
                await self.closing.wait()
        except BaseException as error:
            self.failure = error
        finally:
            self.session = None
            self.ready.set()

    async def close(self) -> None:
        """Fecha a sessão e espera o filho terminar; o transporte mata o processo no prazo."""
        self.closing.set()
        owner, self.owner = self.owner, None
        if owner is not None:
            try:
                await asyncio.wait_for(owner, CHILD_EXIT_TIMEOUT_SECONDS)
            except (TimeoutError, asyncio.CancelledError, Exception):
                owner.cancel()
        self.session = None

    def anthropic_tools(self) -> list[dict[str, Any]]:
        return [tool.document() for tool in self.catalog]

    def rejection(self, name: str, arguments: object) -> str | None:
        """Motivo para não despachar; None quando a chamada pode ir ao servidor."""
        tool = next((item for item in self.catalog if item.name == name), None)
        if tool is None:
            return f"Unknown tool: {name}. Only the advertised Lastro MCP catalog is available."
        if not isinstance(arguments, dict):
            return f"Invalid arguments for {name}: expected a JSON object."
        data = cast(dict[str, Any], arguments)
        if CONTROL_ARGUMENTS & set(data):
            return (
                "approval_proof is reserved for trusted adapters and cannot be sent by the model."
            )
        try:
            jsonschema.validate(data, tool.input_schema)
        except jsonschema.ValidationError as error:
            location = ".".join(str(part) for part in error.absolute_path) or "arguments"
            return f"Invalid arguments for {name}: {location}: {error.validator}"
        return None

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        if self.session is None or self.lost:
            raise McpTransportError("MCP session is closed")
        try:
            result = await self.session.call_tool(name, arguments)
        except Exception as error:
            self.lost = True
            raise McpTransportError(type(error).__name__) from error
        texts = [item.text for item in result.content if isinstance(item, TextContent)]
        text = "\n".join(texts)
        data: dict[str, Any] | None = None
        with contextlib.suppress(ValueError):
            parsed: object = json.loads(text)
            if isinstance(parsed, dict):
                data = cast(dict[str, Any], parsed)
        return ToolOutcome(text=text, data=data, is_error=bool(result.isError))
