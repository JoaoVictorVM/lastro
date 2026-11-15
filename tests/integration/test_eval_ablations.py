import asyncio
from pathlib import Path
from typing import Any

import pytest

from lastro.browser.tools import BrowserTools
from lastro.config.loader import ProjectContext
from lastro.evaluation.baseline import TOOLS as BASELINE_TOOLS
from lastro.evaluation.conditions import (
    EVALUATION_BLOCK,
    MEMORY_TOOLS,
    EffectiveCondition,
    binding_block,
    derive_bundle,
)
from lastro.evaluation.models import Condition
from lastro.evaluation.workspace import WorkspaceRequest, prepare_workspace, trial_paths
from lastro.history import HistoryStore, RunStatus, TextMission, open_history


def run_with(history_context: ProjectContext, condition: Condition) -> tuple[HistoryStore, str]:
    history = open_history(history_context)
    block = binding_block(
        EffectiveCondition.of(condition), suite_id="eval_x", trial_id="trial_x", bundle_sha256=None
    )
    run_id = history.create_run(
        history_context,
        TextMission("Explorar cadastro"),
        skill_bundle_version="c" * 64,
        executor_snapshot={EVALUATION_BLOCK: block},
    ).record.id
    return history, run_id


def test_oracles_disabled_at_collection_and_tool_layer(history_context: ProjectContext) -> None:
    history, run_id = run_with(history_context, Condition.NO_ORACLES)
    tools = BrowserTools(history_context, history, run_id)
    assert tools.condition is not None and not tools.condition.oracles
    assert tools.session.oracles.disabled
    tools.session.oracles.capture(
        "console_error", page=None, url="https://homolog.example.test/x", text="TypeError: boom"
    )
    assert history.list_oracle_signals(run_id) == []
    history.finish_run(run_id, RunStatus.COMPLETED, "done")
    full_history, full_run = run_with(history_context, Condition.FULL)
    assert not BrowserTools(history_context, full_history, full_run).session.oracles.disabled


def test_memory_has_no_read_write_or_consolidation(history_context: ProjectContext) -> None:
    from lastro.native.graph import NativeAgent
    from lastro.native.mcp import expected_catalog

    history, run_id = run_with(history_context, Condition.NO_MEMORY)
    tools = BrowserTools(history_context, history, run_id)
    assert tools.disabled_tools == MEMORY_TOOLS
    for name in sorted(MEMORY_TOOLS):
        result = asyncio.run(tools.call(name, {"flow": "cadastro"}))
        assert result["error"] == "tool_unavailable"
    names = {tool.name for tool in expected_catalog(disabled=MEMORY_TOOLS)}
    assert not names & MEMORY_TOOLS

    class Boom:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError("memory node must not call the model")

    agent = NativeAgent(
        history=history,
        run_id=run_id,
        adapter=Boom(),  # type: ignore[arg-type]
        gateway=Boom(),  # type: ignore[arg-type]
        human=Boom(),  # type: ignore[arg-type]
        settings=Boom(),  # type: ignore[arg-type]
        system=[],
        write=lambda line: None,
        memory_enabled=False,
    )
    state = asyncio.run(agent.consult_memory({"messages": []}))
    assert state.get("memory_consulted") is True


def test_reproduction_ablation_spawns_no_child(history_context: ProjectContext) -> None:
    from lastro.reproduction.coordinator import ReproductionCoordinator, ReproductionError
    from lastro.reproduction.trigger import should_reproduce

    history, run_id = run_with(history_context, Condition.NO_REPRODUCTION)
    history.finish_run(run_id, RunStatus.COMPLETED, "done")
    record = history.get_run(run_id)
    assert not should_reproduce(history_context, record)
    coordinator = ReproductionCoordinator(
        history_context, history, write=lambda line: None, launchers={}
    )
    with pytest.raises(ReproductionError, match="disabled"):
        coordinator.run(run_id)
    assert len(history.list_runs()) == 1


def test_native_graph_obeys_heuristic_ablation() -> None:
    from lastro.native.prompts import Bundle, system_blocks

    derived = derive_bundle(EffectiveCondition.of(Condition.NO_HEURISTICS))
    assert derived is not None
    blocks = system_blocks(Bundle(sha256=derived.sha256, text=derived.text()))
    text = " ".join(block["text"] for block in blocks).lower()
    assert "qa-heuristics" not in text and "heuristic" not in text
    full = derive_bundle(EffectiveCondition.of(Condition.FULL))
    assert full is not None and "qa-heuristics" in full.text()


def test_baseline_has_no_lastro_skills_or_policy(tmp_path: Path) -> None:
    paths = trial_paths(tmp_path, "eval_x", "trial_x", 1)
    request = WorkspaceRequest(
        project_id="p",
        executor="claude-code",
        effective=EffectiveCondition.of(Condition.BASELINE),
        bundle=None,
        mission_text="# Objetivo\n\nTestar.\n",
        criteria_ids=("persistence",),
        demo_url="http://127.0.0.1:8123",
    )
    prepare_workspace(paths, request)
    files = {item.relative_to(paths.workspace).as_posix() for item in paths.workspace.rglob("*")}
    assert not any(name.startswith("skills") for name in files)
    assert "lastro.toml" not in files and "CLAUDE.md" not in files
    mcp = (paths.workspace / ".mcp.json").read_text(encoding="utf-8")
    assert "lastro-eval-browser" in mcp and "lastro-browser" not in mcp.replace(
        "lastro-eval-browser", ""
    )
    for forbidden in ("approval", "knowledge", "oracle", "report_finding", "end_run", "shell"):
        assert not any(forbidden in name for name in BASELINE_TOOLS)
