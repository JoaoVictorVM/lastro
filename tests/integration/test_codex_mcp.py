import asyncio
import tomllib
from pathlib import Path

import pytest

from lastro.browser.server import PinnedRunError, pinned_context
from lastro.browser.tools import TOOLS
from lastro.mission import verify_bundle
from lastro.native.mcp import McpSessionAdapter, compare_catalog, expected_catalog

from .codex_support import CodexProject, make_project


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CodexProject:
    monkeypatch.chdir(tmp_path)
    return make_project(tmp_path)


def _session_server(project: CodexProject) -> dict[str, object]:
    for item in project.call()["argv"]:
        if item.startswith("mcp_servers.lastro="):
            return tomllib.loads("x=" + item.split("=", 1)[1])["x"]
    raise AssertionError("pinned server not found")


def test_same_catalog_and_schemas(project: CodexProject) -> None:
    run_id = project.create_run()
    project.history().claim_launch(run_id)

    async def catalog() -> list[object]:
        adapter = McpSessionAdapter(
            project_dir=project.directory,
            config_path=project.directory / "lastro.toml",
            profile="local",
            run_id=run_id,
        )
        try:
            return list(await adapter.open())
        finally:
            await adapter.close()

    found = asyncio.run(catalog())
    assert compare_catalog(found, expected_catalog()) == []  # type: ignore[arg-type]


def test_same_skill_bundle_hash(project: CodexProject) -> None:
    run_id = project.create_run()
    record = project.history().get_run(run_id)
    assert record.skill_bundle_version == verify_bundle(project.directory).sha256


def test_session_receives_the_verified_bundle(project: CodexProject) -> None:
    run_id = project.create_run()
    project.launch(run_id)
    instructions = next(
        part for part in project.call()["argv"] if part.startswith("developer_instructions=")
    )
    text = tomllib.loads("x=" + instructions.split("=", 1)[1])["x"]
    for name in ("explore.md", "qa-heuristics.md", "reproduce.md"):
        assert f'file="{name}"' in text
    explore = (project.directory / "skills" / "lastro" / "explore.md").read_text(encoding="utf-8")
    assert explore.strip() in text


def test_pinned_run_and_profile(project: CodexProject) -> None:
    run_id = project.create_run()
    project.launch(run_id)

    server = _session_server(project)
    args = server["args"]
    assert isinstance(args, list)
    assert args[args.index("--run-id") + 1] == run_id
    assert args[args.index("--profile") + 1] == "local"
    assert server["enabled_tools"] == sorted(TOOLS)
    assert server["required"] is True


def test_browser_refuses_run_before_the_claim(project: CodexProject) -> None:
    run_id = project.create_run()
    context = project.context()
    with pytest.raises(PinnedRunError, match="not active"):
        pinned_context(context, project.history(), run_id)

    project.history().claim_launch(run_id)
    _, run = pinned_context(context, project.history(), run_id)
    assert run.executor_name == "codex"


def test_mcp_required_at_startup(project: CodexProject) -> None:
    run_id = project.create_run()
    project.behave(stderr="required MCP servers failed to initialize\n", exit=1)
    assert project.launch(run_id).reason == "mcp_unavailable"
    project.outputs.clear()


def test_no_credentials_in_child_context(project: CodexProject) -> None:
    run_id = project.create_run()
    project.launch(run_id)
    call = project.call()
    assert "canary-password" not in " ".join(call["argv"])
    assert "canary-password" not in " ".join(call["env"].values())
    assert not any("auth.json" in part and "deny" not in part for part in call["argv"])
