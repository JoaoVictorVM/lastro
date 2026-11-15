"""Porta headless comum aos três executores, para explorar e para julgar.

Reaproveita a infraestrutura existente: `claude -p` como na reprodução, o launcher headless do
Codex e o runner nativo. Só respostas estruturadas reconhecidas viram limite do provedor; erro
desconhecido é crash. Nenhum caminho de executável vem do agente ou da suite.
"""

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from lastro.evaluation.conditions import (
    EVALUATION_BLOCK,
    DerivedBundle,
    EffectiveCondition,
    verify_derived_bundle,
)
from lastro.evaluation.models import (
    EvalError,
    ExecutorIdentity,
    HeadlessResult,
    HeadlessTask,
    StopReason,
    Usage,
    as_map,
)
from lastro.evaluation.workspace import (
    BASELINE_INSTRUCTIONS,
    MISSION_FILE,
    PASSWORD_ENV,
    PROFILE_NAME,
    USERNAME_ENV,
    TrialPaths,
)

GRACE_SECONDS = 120
NUSD_PER_USD = 1_000_000_000
CLAUDE_FULL_PROMPT = (
    "Run the Lastro mission {run_id}. Call mission_context first and follow the Lastro "
    "instructions until end_run."
)
USAGE_CAP_MARKERS = ("usage limit reached", "usage_limit_reached")

type Writer = Callable[[str], None]
type ProcessRunner = Callable[..., Any]


def usd_to_nusd(value: Any) -> int | None:
    try:
        amount = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    if amount < 0:
        return None
    return int(amount * NUSD_PER_USD)


def parse_claude_output(text: str) -> tuple[StopReason, Usage, str | None, str | None]:
    """Resultado do `claude -p --output-format json`: parada, uso, modelo e texto final."""
    document: dict[str, Any] | None = None
    for line in reversed(text.strip().splitlines()):
        with contextlib.suppress(ValueError):
            candidate = json.loads(line)
            if isinstance(candidate, dict):
                document = candidate  # pyright: ignore[reportUnknownVariableType]
                break
    if document is None:
        with contextlib.suppress(ValueError):
            candidate = json.loads(text)
            if isinstance(candidate, dict):
                document = candidate  # pyright: ignore[reportUnknownVariableType]
    if document is None:
        return StopReason.CRASH, Usage(), None, None
    raw_usage = as_map(document.get("usage"))
    models = as_map(document.get("modelUsage"))
    model = next(iter(models), None)
    cost = usd_to_nusd(document["total_cost_usd"]) if "total_cost_usd" in document else None
    usage = Usage(
        input_tokens=raw_usage.get("input_tokens"),
        output_tokens=raw_usage.get("output_tokens"),
        cost_nusd=cost,
        cost_source="claude-code" if cost is not None else None,
    )
    result = document.get("result")
    final = result if isinstance(result, str) else None
    if document.get("is_error") is True:
        status = document.get("api_error_status")
        lowered = (final or "").lower()
        if status == 429:
            return StopReason.RATE_LIMIT, usage, model, final
        if any(marker in lowered for marker in USAGE_CAP_MARKERS):
            return StopReason.USAGE_CAP, usage, model, final
        return StopReason.CRASH, usage, model, final
    return StopReason.COMPLETED, usage, model, final


@dataclass(frozen=True)
class TrialLaunch:
    task: HeadlessTask
    paths: TrialPaths
    effective: EffectiveCondition
    bundle: DerivedBundle | None
    binding: dict[str, Any] | None
    credentials: tuple[str, str]
    max_minutes: int
    max_actions: int
    system_identity: str | None = None
    on_run: Callable[[str], None] = lambda run_id: None


@runtime_checkable
class HeadlessExecutor(Protocol):
    name: str

    def identity(self) -> ExecutorIdentity:
        """Preflight sem chamada paga: instalação, versão e modelo solicitado."""
        ...

    def launch(self, launch: TrialLaunch, cancel: threading.Event) -> HeadlessResult: ...


@runtime_checkable
class JudgePort(Protocol):
    def identity(self) -> ExecutorIdentity: ...

    def ask(self, prompt: str, workdir: Path) -> tuple[str, Usage, str | None]:
        """Sessão nova, sem tools nem histórico do explorador; devolve texto, uso e modelo."""
        ...


def _credentials_env(launch: TrialLaunch) -> dict[str, str]:
    env = dict(os.environ)
    env[USERNAME_ENV], env[PASSWORD_ENV] = launch.credentials
    env.pop("ANTHROPIC_API_KEY", None) if launch.task.executor != "native" else None
    return env


@dataclass
class LastroRun:
    """Run do Lastro no histórico isolado da tentativa, com a condição vinculada."""

    launch: TrialLaunch
    project: Any = None
    history: Any = None
    run_id: str | None = None

    def create(self, executor: str, version: str, extra: Mapping[str, Any]) -> str:
        from lastro.config.loader import load_project
        from lastro.history import ExecutorInfo, FileMission, open_history

        workspace = self.launch.paths.workspace
        os.environ[USERNAME_ENV], os.environ[PASSWORD_ENV] = self.launch.credentials
        self.project = load_project(workspace / "lastro.toml", PROFILE_NAME, cwd=workspace)
        self.history = open_history(self.project)
        bundle_sha = self.launch.bundle.sha256 if self.launch.bundle is not None else None
        created = self.history.create_run(
            self.project,
            FileMission(workspace / MISSION_FILE),
            ExecutorInfo(name=executor, version=version),
            skill_bundle_version=bundle_sha,
            executed_system_version=self.launch.system_identity,
            executor_snapshot={EVALUATION_BLOCK: self.launch.binding, **extra},
        )
        self.run_id = created.record.id
        self.launch.on_run(created.record.id)
        return created.record.id

    def ensure_terminal(self) -> None:
        if self.history is None or self.run_id is None:
            return
        from lastro.history import HistoryError, RunStatus

        with contextlib.suppress(HistoryError):
            record = self.history.get_run(self.run_id)
            if not record.status.is_terminal:
                self.history.finish_run(self.run_id, RunStatus.INTERRUPTED, "eval_supervisor")

    def reproduce(self, write: Writer) -> None:
        """F13 depois da exploração, no mesmo demo e sem reset entre exploração e reprodução."""
        if self.history is None or self.run_id is None or not self.launch.effective.reproduction:
            return
        from lastro.history import HistoryError
        from lastro.reproduction.coordinator import ReproductionCoordinator, ReproductionError

        with contextlib.suppress(HistoryError, ReproductionError):
            if self.history.eligible_findings(self.run_id):
                ReproductionCoordinator(self.project, self.history, write=write).run(self.run_id)


def _terminate(process: subprocess.Popen[bytes]) -> None:
    from lastro_demo import _process

    with contextlib.suppress(Exception):
        _process.force_kill(process.pid)
    with contextlib.suppress(Exception):
        process.wait(timeout=10)


def run_supervised(
    arguments: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log: Path,
    timeout: float,
    cancel: threading.Event,
    spawn: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
) -> tuple[int | None, bool]:
    """Executa com stdin fechado; cancelamento ou prazo encerram a árvore de processos."""
    with log.open("wb") as handle:
        process = spawn(
            arguments, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=handle, stderr=handle
        )
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if cancel.is_set() or time.monotonic() > deadline:
                _terminate(process)
                return process.returncode, True
            time.sleep(0.25)
    return process.returncode, False


def _version(arguments: list[str]) -> str:
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        completed = subprocess.run(
            arguments, capture_output=True, text=True, timeout=30, check=False
        )
        text = (completed.stdout or completed.stderr).strip().splitlines()
        if completed.returncode == 0 and text:
            return text[0][:120]
    return "unknown"


@dataclass
class ClaudeCodeExecutor:
    name: str = "claude-code"
    model: str | None = None
    write: Writer = lambda line: None
    spawn: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen
    _identity: ExecutorIdentity | None = None

    def binary(self) -> str:
        found = shutil.which("claude")
        if found is None:
            raise EvalError("executor_unavailable", "claude not found in PATH")
        return found

    def identity(self) -> ExecutorIdentity:
        if self._identity is None:
            binary = self.binary()
            self._identity = ExecutorIdentity(
                self.name, _version([binary, "--version"]), model_requested=self.model
            )
        return self._identity

    def _arguments(self, prompt: str) -> list[str]:
        arguments = [
            self.binary(),
            "-p",
            prompt,
            "--output-format",
            "json",
            "--mcp-config",
            ".mcp.json",
            "--strict-mcp-config",
        ]
        if self.model:
            arguments += ["--model", self.model]
        return arguments

    def launch(self, launch: TrialLaunch, cancel: threading.Event) -> HeadlessResult:
        identity = self.identity()
        run = LastroRun(launch)
        if launch.effective.lastro:
            run_id = run.create(self.name, identity.version, {})
            prompt = CLAUDE_FULL_PROMPT.format(run_id=run_id)
        else:
            run_id = None
            workspace = launch.paths.workspace
            prompt = (workspace / BASELINE_INSTRUCTIONS).read_text(encoding="utf-8")
            prompt += "\n\n# Mission\n\n" + (workspace / MISSION_FILE).read_text(encoding="utf-8")
        log = launch.paths.logs / "executor.log"
        try:
            returncode, stopped = run_supervised(
                self._arguments(prompt),
                cwd=launch.paths.workspace,
                env=_credentials_env(launch),
                log=log,
                timeout=launch.max_minutes * 60 + GRACE_SECONDS,
                cancel=cancel,
                spawn=self.spawn,
            )
        except OSError as exc:
            raise EvalError("executor_unavailable", f"claude failed to start: {exc}") from None
        finally:
            run.ensure_terminal()
        text = log.read_text(encoding="utf-8", errors="replace")
        status, usage, model, _ = parse_claude_output(text)
        if stopped:
            status = StopReason.INTERRUPTED
        elif returncode not in (0, None) and status == StopReason.COMPLETED:
            status = StopReason.CRASH
        if status == StopReason.COMPLETED:
            run.reproduce(self.write)
        return HeadlessResult(
            status=status,
            executor=ExecutorIdentity(self.name, identity.version, self.model, model),
            run_ref=run_id,
            usage=usage,
            logs=(launch.paths.relative(log),),
        )


@dataclass
class CodexExecutor:
    name: str = "codex"
    model: str | None = None
    write: Writer = lambda line: None
    _identity: ExecutorIdentity | None = None

    def identity(self) -> ExecutorIdentity:
        if self._identity is None:
            from lastro.codex.preflight import CodexPreflightError, probe_install

            try:
                install = probe_install()
            except CodexPreflightError as exc:
                raise EvalError("executor_unavailable", str(exc)) from None
            self._identity = ExecutorIdentity(self.name, install.version, self.model)
        return self._identity

    def launch(self, launch: TrialLaunch, cancel: threading.Event) -> HeadlessResult:
        if not launch.effective.lastro:
            # O exec do Codex não oferece aqui uma forma verificável de limitar a sessão ao
            # browser baseline; a condição é recusada antes de abrir o agente.
            raise EvalError(
                "isolation_unavailable", "codex baseline cannot be restricted to the demo browser"
            )
        from lastro.codex.launcher import LaunchRefused, executor_snapshot, prepare_codex
        from lastro.codex.launcher import launch as codex_launch
        from lastro.codex.models import LaunchMode
        from lastro.codex.preflight import CodexPreflightError

        identity = self.identity()
        run = LastroRun(launch)
        condition = launch.effective.condition

        def verifier(project_dir: Path) -> str:
            return verify_derived_bundle(
                project_dir / "skills" / "lastro", expected_condition=condition
            ).sha256

        try:
            from lastro.config.loader import load_project

            workspace = launch.paths.workspace
            project = load_project(workspace / "lastro.toml", PROFILE_NAME, cwd=workspace)
            prepared = prepare_codex(project, bundle_verifier=verifier)
            run_id = run.create(self.name, identity.version, executor_snapshot(prepared))
            watcher = threading.Thread(
                target=_cancel_watch, args=(cancel, run), daemon=True, name="eval-codex-cancel"
            )
            watcher.start()
            codex_launch(
                run.project,
                run.history,
                run_id,
                LaunchMode.HEADLESS,
                write=self.write,
                bundle_verifier=verifier,
            )
        except (LaunchRefused, CodexPreflightError) as exc:
            raise EvalError("executor_unavailable", str(exc)) from None
        finally:
            run.ensure_terminal()
        record = run.history.get_run(run_id)
        status = StopReason.COMPLETED
        if cancel.is_set():
            status = StopReason.INTERRUPTED
        elif record.status_reason in {"usage_limit", "rate_limited"}:
            status = StopReason.USAGE_CAP
        if status == StopReason.COMPLETED:
            run.reproduce(self.write)
        return HeadlessResult(status=status, executor=identity, run_ref=run_id)


def _cancel_watch(cancel: threading.Event, run: LastroRun) -> None:
    """Cancelamento do supervisor vira o pedido durável existente, honrado pelo owner."""
    cancel.wait()
    if run.history is not None and run.run_id is not None:
        with contextlib.suppress(Exception):
            run.history.request_cancellation(run.run_id, origin="eval")


@dataclass
class NativeExecutor:
    name: str = "native"
    model: str | None = None
    write: Writer = lambda line: None
    provider_factory: Callable[[str], Any] | None = None
    _identity: ExecutorIdentity | None = None

    def identity(self) -> ExecutorIdentity:
        if self._identity is None:
            from lastro.native.runner import missing_dependencies

            if missing_dependencies():
                raise EvalError("executor_unavailable", "native extra is not installed")
            if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
                raise EvalError("executor_unavailable", "ANTHROPIC_API_KEY is not set")
            from lastro import get_version

            self._identity = ExecutorIdentity(self.name, get_version(), self.model)
        return self._identity

    def _provider(self) -> Any:
        from lastro.native import runner

        factory = self.provider_factory or runner.PROVIDER_FACTORY
        return factory(os.environ["ANTHROPIC_API_KEY"])

    def launch(self, launch: TrialLaunch, cancel: threading.Event) -> HeadlessResult:
        identity = self.identity()
        if not launch.effective.lastro:
            return self._baseline(launch, cancel, identity)
        from lastro.native.prompts import Bundle
        from lastro.native.runner import NativeRunner, Preflight

        assert launch.bundle is not None
        run = LastroRun(launch)
        run_id = run.create(self.name, identity.version, {})
        settings = run.project.profile.native
        if self.model:
            settings = settings.model_copy(update={"model": self.model})
        prepared = Preflight(
            settings=settings,
            api_key=os.environ["ANTHROPIC_API_KEY"],
            bundle=Bundle(sha256=launch.bundle.sha256, text=launch.bundle.text()),
        )
        runner = NativeRunner(
            run.project,
            run.history,
            run_id,
            prepared,
            self._provider(),
            write=self.write,
            interactive=False,
        )
        watcher = threading.Thread(target=_cancel_watch, args=(cancel, run), daemon=True)
        watcher.start()
        try:
            outcome = asyncio.run(runner.run())
        finally:
            run.ensure_terminal()
        status = StopReason.COMPLETED
        if cancel.is_set():
            status = StopReason.INTERRUPTED
        elif outcome.reason in {"cost_cap", "suite_cost_cap"}:
            status = StopReason.COST_CAP
        elif outcome.reason in {"rate_limited", "usage_limit"}:
            status = StopReason.RATE_LIMIT
        if status == StopReason.COMPLETED:
            run.reproduce(self.write)
        usage = Usage()
        if outcome.usage is not None:
            usage = Usage(
                input_tokens=outcome.usage.input_tokens,
                output_tokens=outcome.usage.output_tokens,
                cost_nusd=usd_to_nusd(outcome.usage.committed_usd),
                cost_source="native-ledger",
            )
        return HeadlessResult(
            status=status,
            executor=ExecutorIdentity(self.name, identity.version, self.model, settings.model),
            run_ref=run_id,
            usage=usage,
        )

    def _baseline(
        self, launch: TrialLaunch, cancel: threading.Event, identity: ExecutorIdentity
    ) -> HeadlessResult:
        from lastro.evaluation.baseline import neutral_loop

        workspace = launch.paths.workspace
        task_text = (workspace / BASELINE_INSTRUCTIONS).read_text(encoding="utf-8")
        task_text += "\n\n# Mission\n\n" + (workspace / MISSION_FILE).read_text(encoding="utf-8")
        model = self.model or "claude-sonnet-5-5"

        async def run() -> Any:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            from mcp.types import TextContent

            params = StdioServerParameters(
                command="lastro-eval-browser", args=["--trial-dir", str(launch.paths.root)]
            )
            async with (
                stdio_client(params) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()

                async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                    result = await session.call_tool(name, arguments)
                    texts = [item.text for item in result.content if isinstance(item, TextContent)]
                    return json.loads(texts[0]) if texts else {"error": "empty_result"}

                def check(step: int) -> None:
                    if cancel.is_set():
                        raise asyncio.CancelledError

                return await neutral_loop(
                    self._provider(),
                    model,
                    task_text,
                    call,
                    max_steps=launch.max_actions,
                    before_call=check,
                )

        try:
            asyncio.run(run())
            status = StopReason.COMPLETED
        except asyncio.CancelledError:
            status = StopReason.INTERRUPTED
        return HeadlessResult(
            status=status,
            executor=ExecutorIdentity(self.name, identity.version, self.model, model),
        )


@dataclass
class ClaudeJudge:
    model: str | None = None
    run: ProcessRunner = subprocess.run
    _identity: ExecutorIdentity | None = None

    def identity(self) -> ExecutorIdentity:
        if self._identity is None:
            binary = shutil.which("claude")
            if binary is None:
                raise EvalError("judge_unavailable", "claude not found in PATH")
            self._identity = ExecutorIdentity(
                "claude-code", _version([binary, "--version"]), self.model
            )
        return self._identity

    def ask(self, prompt: str, workdir: Path) -> tuple[str, Usage, str | None]:
        binary = shutil.which("claude") or "claude"
        workdir.mkdir(parents=True, exist_ok=True)
        empty = workdir / "no-mcp.json"
        empty.write_text('{"mcpServers": {}}', encoding="utf-8")
        settings = workdir / ".claude" / "settings.json"
        settings.parent.mkdir(exist_ok=True)
        settings.write_text(
            json.dumps({"permissions": {"deny": ["Bash", "Read", "Write", "Edit", "WebFetch"]}}),
            encoding="utf-8",
        )
        arguments = [
            binary,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--mcp-config",
            str(empty),
            "--strict-mcp-config",
        ]
        if self.model:
            arguments += ["--model", self.model]
        completed = self.run(
            arguments,
            cwd=workdir,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        status, usage, model, final = parse_claude_output(completed.stdout or "")
        if status.pauses_suite:
            raise EvalError(status.value, "judge stopped by provider limit")
        if status != StopReason.COMPLETED or final is None:
            raise EvalError("judge_unavailable", "judge session failed")
        return final, usage, model


@dataclass
class CodexJudge:
    model: str | None = None
    run: ProcessRunner = subprocess.run
    _identity: ExecutorIdentity | None = None

    def identity(self) -> ExecutorIdentity:
        if self._identity is None:
            from lastro.codex.preflight import CodexPreflightError, probe_install

            try:
                install = probe_install()
            except CodexPreflightError as exc:
                raise EvalError("judge_unavailable", str(exc)) from None
            self._identity = ExecutorIdentity("codex", install.version, self.model)
        return self._identity

    def ask(self, prompt: str, workdir: Path) -> tuple[str, Usage, str | None]:
        from lastro.codex.preflight import probe_install

        install = probe_install()
        workdir.mkdir(parents=True, exist_ok=True)
        last = workdir / "judge-last-message.txt"
        arguments = [
            *install.command,
            "exec",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--output-last-message",
            str(last),
        ]
        if self.model:
            arguments += ["--model", self.model]
        completed = self.run(
            [*arguments, prompt],
            cwd=workdir,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        if completed.returncode != 0 or not last.is_file():
            raise EvalError("judge_unavailable", "codex judge session failed")
        return last.read_text(encoding="utf-8"), Usage(), None


@dataclass
class NativeJudge:
    """Requisição sem tools, pelo mesmo provedor do executor nativo."""

    model: str | None = None
    provider_factory: Callable[[str], Any] | None = None
    _identity: ExecutorIdentity | None = None
    calls: list[Any] = field(default_factory=list[Any])

    def identity(self) -> ExecutorIdentity:
        if self._identity is None:
            if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
                raise EvalError("judge_unavailable", "ANTHROPIC_API_KEY is not set")
            from lastro import get_version

            self._identity = ExecutorIdentity("native", get_version(), self.model)
        return self._identity

    def ask(self, prompt: str, workdir: Path) -> tuple[str, Usage, str | None]:
        from lastro.native import runner
        from lastro.native.provider import ModelRequest, ProviderError

        factory = self.provider_factory or runner.PROVIDER_FACTORY
        provider = factory(os.environ["ANTHROPIC_API_KEY"])
        model = self.model or "claude-sonnet-5-5"
        request = ModelRequest(
            model=model,
            system=[{"type": "text", "text": "You are an evaluation judge. Answer with JSON."}],
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            max_tokens=1024,
        )
        try:
            response = asyncio.run(provider.create(request))
        except ProviderError as exc:
            if exc.status == 429:
                raise EvalError("rate_limit", "judge stopped by provider limit") from None
            raise EvalError("judge_unavailable", exc.code) from None
        text = "".join(
            str(block.get("text", "")) for block in response.content if block.get("type") == "text"
        )
        usage = response.usage
        return (
            text,
            Usage(
                input_tokens=getattr(usage, "input_tokens", None),
                output_tokens=getattr(usage, "output_tokens", None),
            ),
            model,
        )


def default_executor(name: str, model: str | None, write: Writer) -> HeadlessExecutor:
    if name == "claude-code":
        return ClaudeCodeExecutor(model=model, write=write)
    if name == "codex":
        return CodexExecutor(model=model, write=write)
    if name == "native":
        return NativeExecutor(model=model, write=write)
    raise EvalError("executor_unavailable", f"unknown executor {name}")


def default_judge(name: str, model: str | None) -> JudgePort:
    if name == "claude-code":
        return ClaudeJudge(model=model)
    if name == "codex":
        return CodexJudge(model=model)
    if name == "native":
        return NativeJudge(model=model)
    raise EvalError("judge_unavailable", f"unknown judge executor {name}")


async def scripted_login(
    demo_url: str, email: str, password: str, tenant: str, storage_state: Path
) -> None:
    """Autenticação do supervisor para o baseline; a credencial nunca chega ao agente."""
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(demo_url + "/login")
            await page.fill('[name="username"]', email)
            await page.fill('[name="password"]', password)
            await page.click('button[type="submit"]')
            if "/select-clinic" in page.url:
                await page.select_option('[name="tenant"]', tenant)
                await page.click('button[type="submit"]')
            await context.storage_state(path=str(storage_state))
        finally:
            await browser.close()


def task_for(
    task_id: str,
    executor: str,
    model: str | None,
    workspace: Path,
    evaluation: Mapping[str, Any] | None,
) -> HeadlessTask:
    from lastro.evaluation.models import GateMode, TaskPurpose

    return HeadlessTask(
        task_id=task_id,
        purpose=TaskPurpose.EVALUATION,
        executor=executor,
        model=model,
        workspace=workspace,
        mission_ref=MISSION_FILE,
        gate_mode=GateMode.SCRIPTED,
        evaluation=evaluation,
    )
