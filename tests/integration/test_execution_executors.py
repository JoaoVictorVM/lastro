import asyncio
import json
import sys
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from lastro.codex.lifecycle import EXIT_LIMIT, exit_code_for
from lastro.config.loader import ProjectContext
from lastro.history import RunStatus, TextMission, open_history


def test_codex_observes_owner_stop(history_context: ProjectContext) -> None:
    from lastro.codex.lifecycle import RunCloser
    from lastro.execution.supervisor import stop_run

    history = open_history(history_context)
    run_id = history.create_run(history_context, TextMission("Explore")).record.id
    stopped = stop_run(
        history, run_id, status=RunStatus.LIMIT_REACHED, reason="action_limit", origin="owner"
    )
    assert stopped.status == "limit_reached"
    closer = RunCloser(history_context, history, run_id, write=lambda _: None)
    # O launcher respeita o término do owner e reaproveita o relatório parcial existente.
    closure = closer.finish_locally(RunStatus.INTERRUPTED, "executor_exited")
    assert closure.report_kind == "partial" and closure.report_path == stopped.report_path
    record = history.get_run(run_id)
    assert (record.status, record.status_reason) == (RunStatus.LIMIT_REACHED, "action_limit")
    assert exit_code_for(record.status.value, record.status_reason) == EXIT_LIMIT


def test_native_stops_without_extra_model_call(history_context: ProjectContext) -> None:
    from lastro.native.graph import NativeAgent

    history = open_history(history_context)
    run_id = history.create_run(history_context, TextMission("Explore")).record.id
    agent = SimpleNamespace(history=history, run_id=run_id)
    assert NativeAgent.run_terminal(agent) is None  # type: ignore[arg-type]
    history.latch_stop(run_id, RunStatus.LIMIT_REACHED, "no_progress")
    # Com o latch gravado e a run ainda não terminal, o grafo já vai para o encerramento.
    assert history.get_run(run_id).status == RunStatus.RUNNING
    assert NativeAgent.run_terminal(agent) == ("limit_reached", "no_progress")  # type: ignore[arg-type]


def test_mcp_owner_enforces_limits_without_executor_cooperation(
    make_auth_project: Callable[..., Any], profile_for: Callable[..., str], demo_server: Any
) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.types import TextContent

    profile = profile_for("demo", demo_server.url) + (
        "\n[profiles.demo.run_limits]\nmax_actions = 2\nmax_no_progress_actions = 50\n"
    )
    project = make_auth_project(profile)
    context = project.context()
    history = open_history(context)
    run_id = history.create_run(context, TextMission("Explore")).record.id

    async def scenario() -> list[dict[str, Any]]:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "lastro.browser.server", "--profile", "demo"],
            cwd=str(project.config_path.parent),
        )
        results: list[dict[str, Any]] = []
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as client:
            await client.initialize()
            for index in range(3):
                response = await client.call_tool(
                    "navigate", {"url": f"https://evil.example/{index}"}
                )
                content = response.content[0]
                assert isinstance(content, TextContent)
                results.append(json.loads(content.text))
            for _ in range(150):
                if history.get_run(run_id).status.is_terminal:
                    break
                await asyncio.sleep(0.1)
        return results

    results = asyncio.run(scenario())
    assert results[1]["execution_notice"]["reason"] == "action_limit"
    assert results[2]["decision"] == "deny" and results[2]["reason"] == "write_lock"
    record = history.get_run(run_id)
    assert (record.status, record.status_reason) == (RunStatus.LIMIT_REACHED, "action_limit")
    assert history.execution_state(run_id).actions_reserved == 2
