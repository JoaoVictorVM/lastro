import asyncio
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from lastro.cli.app import app
from lastro.history import EventType, RunStatus, open_history
from lastro.native import runner as native_runner
from lastro.native.provider import API_KEY_MESSAGE, ProviderError

PLAN: dict[str, Any] = {
    "objectives": ["Conferir a lista de funcionários"],
    "scope": ["Lista de funcionários"],
    "criteria": [{"text": "A lista de funcionários é exibida", "source": "mission_explicit"}],
    "gaps": [],
    "out_of_scope": ["Edição"],
    "charter": {
        "target": "employee list",
        "resources": ["demo application"],
        "information": "display defects",
        "heuristics": ["crud_consistency"],
    },
}


def demo_script(scripted: Any, base_url: str) -> list[Callable[[Any], list[dict[str, Any]]]]:
    def plan_id(request: Any) -> str:
        plan = next(item for item in scripted.results(request) if "criterion_ids" in item)
        return str(plan["criterion_ids"][0])

    def artifact(request: Any) -> str:
        shot = next(item for item in reversed(scripted.results(request)) if "artifact_id" in item)
        return str(shot["artifact_id"])

    return [
        lambda request: scripted.tool_use("submit_mission_plan", PLAN),
        lambda request: [{"type": "text", "text": "Memory is not needed for this read."}],
        lambda request: scripted.tool_use("navigate", {"url": base_url + "/employees"}),
        lambda request: scripted.tool_use("screenshot", {}),
        lambda request: scripted.tool_use(
            "record_criterion_result",
            {
                "criterion_id": plan_id(request),
                "outcome": "met",
                "evidence_artifact_ids": [artifact(request)],
                "note": "A lista aparece com os funcionários do tenant.",
                "reason": "positive_behavior_observed",
            },
        ),
        lambda request: scripted.tool_use(
            "end_run", {"outcome": "completed", "reason": "Mission finished"}
        ),
    ]


def start(runner: CliRunner, *extra: str) -> Any:
    return runner.invoke(
        app,
        ["--profile", "demo", "run", "start", *extra, "--mission", "Confira a lista"],
        catch_exceptions=False,
    )


def test_native_completes_demo_with_scripted_provider(
    native_demo: Any,
    scripted: Any,
    demo_server: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = scripted.Model(demo_script(scripted, demo_server.url))
    monkeypatch.setattr(native_runner, "PROVIDER_FACTORY", lambda key: model)
    result = start(CliRunner())
    assert result.exit_code == 0, result.output
    history = open_history(native_demo.context())
    run = history.list_runs()[0].record
    assert (run.status, run.executor_name) == (RunStatus.COMPLETED, "native")
    assert run.config_snapshot["executor"] == "native"
    criteria = history.list_criterion_results(run.id)
    assert [item.outcome.value for item in criteria] == ["met"]
    assert "Mission plan (revision 1)" in result.output
    assert "Tokens: input 9000" in result.output
    types = [event.event_type for event in history.list_events(run.id)]
    assert types[:2] == [EventType.RUN_STARTED, EventType.NATIVE_STARTED]
    assert types.count(EventType.MODEL_CALL_COMPLETED) == 6
    # Mesmo browser em toda a run: um único login para todas as ferramentas.
    assert demo_server.access_log.count(("POST", "/login")) == 1
    system = model.requests[0].system
    assert system == model.requests[-1].system
    assert system[-1]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}


def test_missing_key_prevents_run_creation(
    native_demo: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    result = start(CliRunner())
    assert result.exit_code == 2
    assert API_KEY_MESSAGE in result.stderr
    history = open_history(native_demo.context())
    assert history.list_runs() == []


def test_invalid_key_uses_documented_message(
    native_demo: Any, scripted: Any, demo_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Rejecting:
        async def count_tokens(self, request: Any) -> int:
            raise ProviderError("auth", "api_auth", status=401, not_charged=True)

        async def create(self, request: Any) -> Any:
            raise AssertionError("no generation during preflight")

    monkeypatch.setattr(native_runner, "PROVIDER_FACTORY", lambda key: Rejecting())
    result = start(CliRunner())
    assert result.exit_code == 2
    assert result.stderr.strip() == API_KEY_MESSAGE
    assert "sk-ant-test" not in result.output
    assert open_history(native_demo.context()).list_runs() == []
    assert demo_server.access_log == []

    class Offline(Rejecting):
        async def count_tokens(self, request: Any) -> int:
            raise ProviderError("transient", "connection_error")

    waits: list[float] = []

    async def no_wait(delay: float) -> None:
        waits.append(delay)

    with pytest.raises(native_runner.PreflightError) as offline:
        asyncio.run(
            native_runner.preflight(
                native_demo.context().with_executor("native"), lambda key: Offline(), sleep=no_wait
            )
        )
    assert API_KEY_MESSAGE not in str(offline.value)
    assert "unavailable" in str(offline.value)
    assert len(waits) == 3


def test_summary_matches_persisted_usage(
    native_demo: Any, scripted: Any, demo_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = scripted.Model(demo_script(scripted, demo_server.url))
    monkeypatch.setattr(native_runner, "PROVIDER_FACTORY", lambda key: model)
    result = start(CliRunner())
    assert result.exit_code == 0, result.output
    history = open_history(native_demo.context())
    run_id = history.list_runs()[0].record.id
    usage = history.native_usage(run_id)
    assert usage is not None
    assert f"input {usage.total_input_tokens}" in result.output
    assert f"output {usage.output_tokens}" in result.output
    assert f"cache read {usage.cache_read_tokens}, cache write {usage.cache_write_tokens}" in (
        result.output
    )
    from lastro.native.pricing import format_usd

    assert f"USD {format_usd(usage.confirmed_nusd)} confirmed" in result.output
    assert usage.uncertain_calls == 0


def test_base_install_does_not_import_native_extra() -> None:
    code = (
        "import sys\n"
        "import lastro.cli.app, lastro.cli.run_cmd, lastro.doctor.runner, lastro.history\n"
        "import lastro.browser.server, lastro.native, lastro.native.runner\n"
        "loaded = [m for m in ('langgraph', 'anthropic', 'langchain_core') if m in sys.modules]\n"
        "print(','.join(loaded))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=120
    )
    assert result.stdout.strip() == ""


def test_init_and_doctor_do_not_require_claude(
    runner: CliRunner,
    workdir: Path,
    doctor_env: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lastro.doctor.runner import run_doctor
    from lastro.native import missing_dependencies

    if missing_dependencies():
        pytest.skip("native extra not installed")

    result = runner.invoke(
        app,
        [
            "init",
            "--executor",
            "native",
            "--name",
            "homolog",
            "--target-url",
            "https://homolog.example.test",
            "--login-url",
            "https://homolog.example.test/login",
            "--tenant",
            "acme",
        ],
    )
    assert result.exit_code == 0, result.output
    assert not (workdir / "CLAUDE.md").exists()
    assert not (workdir / ".claude").exists()
    assert not (workdir / ".mcp.json").exists()
    assert "ANTHROPIC_API_KEY=" in (workdir / ".env.example").read_text(encoding="utf-8")
    assert 'executor = "native"' in (workdir / "lastro.toml").read_text(encoding="utf-8")
    monkeypatch.setenv("HOMOLOG_USERNAME", "u")
    monkeypatch.setenv("HOMOLOG_PASSWORD", "p")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    env = doctor_env(which=lambda name: None if name == "claude" else f"/usr/bin/{name}")
    checks = {item.number: item for item in run_doctor(env, cwd=workdir)}
    assert checks[4].status.value == "ok", checks[4]
    assert checks[12].status.value == "ok"
    assert checks[13].status.value == "ok"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    missing = {item.number: item for item in run_doctor(env, cwd=workdir)}
    assert "ANTHROPIC_API_KEY" in missing[4].detail
    assert json.dumps(missing[4].detail).count("sk-ant") == 0
