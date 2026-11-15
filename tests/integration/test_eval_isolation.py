import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from lastro.browser.tools import BrowserTools
from lastro.config.loader import ProjectContext
from lastro.evaluation.baseline import (
    ACTIONS_FILE,
    CONTEXT_FILE,
    NEUTRAL_SYSTEM,
    BaselineContext,
    BaselineTools,
    Containment,
    load_context,
    neutral_loop,
    tool_catalog,
)
from lastro.evaluation.conditions import EffectiveCondition, derive_bundle
from lastro.evaluation.models import Condition, EvalError
from lastro.evaluation.suites import catalog_bytes, rubric_text
from lastro.evaluation.workspace import (
    WorkspaceRequest,
    prepare_workspace,
    trial_paths,
    verify_isolation,
)

from .test_eval_ablations import run_with


def full_workspace(tmp_path: Path, executor: str = "claude-code") -> Any:
    paths = trial_paths(tmp_path, "eval_x", "trial_x", 1)
    effective = EffectiveCondition.of(Condition.FULL)
    prepare_workspace(
        paths,
        WorkspaceRequest(
            project_id="p",
            executor=executor,
            effective=effective,
            bundle=derive_bundle(effective),
            mission_text="# Objetivo\n\nTestar o cadastro.\n",
            criteria_ids=("persistence",),
            demo_url="http://127.0.0.1:8123",
        ),
    )
    return paths


def test_explorer_cannot_read_catalog_or_review_files(tmp_path: Path) -> None:
    paths = full_workspace(tmp_path)
    contents = "\n".join(
        item.read_text(encoding="utf-8") for item in paths.workspace.rglob("*") if item.is_file()
    )
    assert "expected_symptom" not in contents and "same_symptom" not in contents
    assert rubric_text()[:40] not in contents
    settings = json.loads((paths.workspace / ".claude" / "settings.json").read_text("utf-8"))
    assert {"Read", "Bash", "Grep", "Glob", "WebFetch"} <= set(settings["permissions"]["deny"])
    review = tmp_path / "evals" / "eval_x" / "review"
    assert paths.workspace not in review.parents
    (paths.workspace / "notes.yaml").write_bytes(catalog_bytes())
    with pytest.raises(EvalError) as error:
        verify_isolation(paths, "claude-code")
    assert error.value.code == "isolation_unavailable"
    with pytest.raises(EvalError):
        verify_isolation(paths, "claude-code", private=(paths.workspace / "eval-review.jsonl",))


def test_permissive_executor_settings_are_refused(tmp_path: Path) -> None:
    paths = full_workspace(tmp_path)
    settings_path = paths.workspace / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text("utf-8"))
    settings["permissions"]["deny"].remove("Read")
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    with pytest.raises(EvalError, match="allow Read"):
        verify_isolation(paths, "claude-code")


def test_baseline_refuses_external_target_and_control_routes(tmp_path: Path) -> None:
    for url in ("https://example.com", "http://10.0.0.5:8000", "http://127.0.0.1:8123/app"):
        (tmp_path / CONTEXT_FILE).write_text(
            json.dumps({"demo_url": url, "storage_state": "s.json"}), "utf-8"
        )
        with pytest.raises(EvalError) as error:
            load_context(tmp_path)
        assert error.value.code == "baseline_target_forbidden"
    containment = Containment.for_url("http://127.0.0.1:8123")
    assert containment.allows("http://127.0.0.1:8123/employees?x=1")
    assert not containment.allows("http://127.0.0.1:8123/__demo/reset")
    assert not containment.allows("http://127.0.0.1:9999/employees")
    assert not containment.allows("https://cdn.example.com/lib.js")
    tools = BaselineTools(
        tmp_path, BaselineContext(demo_url="http://127.0.0.1:8123", storage_state="s.json")
    )
    for target in ("/__demo/reset", "http://evil.example/steal", "//evil.example/x"):
        result = asyncio.run(tools.call("navigate", {"path": target}))
        assert result["error"] == "target_forbidden"
    lines = (tmp_path / ACTIONS_FILE).read_text("utf-8").splitlines()
    assert [json.loads(line)["outcome"] for line in lines] == ["denied"] * 3


def test_browser_redirects_and_subrequests_stay_in_demo(tmp_path: Path, demo_server: Any) -> None:
    from playwright.async_api import async_playwright

    state = tmp_path / "state.json"
    state.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    context = BaselineContext(
        demo_url=demo_server.url, storage_state=state.name, start_path="/login"
    )
    tools = BaselineTools(tmp_path, context)

    async def scenario() -> list[str]:
        async with async_playwright() as playwright:
            await tools.attach(playwright)
            try:
                await tools.page.evaluate(
                    "Promise.allSettled([fetch('/__demo/version'), "
                    "fetch('http://example.invalid/x'), fetch('/login')])"
                )
            finally:
                await tools.close()
        return [path for _, path in demo_server.access_log]

    reached = asyncio.run(scenario())
    assert "/__demo/version" not in reached and "/login" in reached
    denied = [
        json.loads(line)
        for line in (tmp_path / ACTIONS_FILE).read_text("utf-8").splitlines()
        if '"denied"' in line
    ]
    assert len(denied) >= 2


class ScriptedProvider:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def create(self, request: Any) -> Any:
        from lastro.native.provider import ModelResponse

        self.requests.append(request)
        if len(self.requests) == 1:
            content = [{"type": "tool_use", "id": "u1", "name": "observe", "input": {}}]
        else:
            output = {"findings": [], "criteria": [], "questions": []}
            content = [{"type": "tool_use", "id": "u2", "name": "submit_output", "input": output}]
        return ModelResponse(content=content, stop_reason="tool_use", usage=None)


def test_native_baseline_uses_neutral_loop() -> None:
    provider = ScriptedProvider()
    calls: list[str] = []

    async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append(name)
        return {"status": "ok"}

    result = asyncio.run(neutral_loop(provider, "model-x", "Teste", call, max_steps=5))
    assert result.submitted and result.steps == 2 and calls == ["observe", "submit_output"]
    request = provider.requests[0]
    assert request.system == [{"type": "text", "text": NEUTRAL_SYSTEM}]
    assert [tool["name"] for tool in request.tools] == [item["name"] for item in tool_catalog()]
    text = json.dumps(request.system) + json.dumps(request.messages)
    for lastro_only in ("mission_context", "skill", "heuristic", "knowledge", "approval"):
        assert lastro_only not in text


def test_eval_flags_cannot_be_set_by_tool_input(history_context: ProjectContext) -> None:
    history, run_id = run_with(history_context, Condition.FULL)
    tools = BrowserTools(history_context, history, run_id)
    assert tools.disabled_tools == frozenset()
    forged = {"flow": "cadastro", "evaluation": {"condition": "-memory"}, "condition": "-memory"}
    result = asyncio.run(tools.call("knowledge_query", forged))
    assert result.get("error") != "tool_unavailable"
    assert tools.disabled_tools == frozenset()
    assert tools.condition == EffectiveCondition.of(Condition.FULL)
