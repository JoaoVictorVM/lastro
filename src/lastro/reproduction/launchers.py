"""Porta de lançamento do reprodutor e adaptadores por executor.

A disponibilidade é verificada antes de abrir a run filha. Um verificador indisponível nunca
cai silenciosamente para outro executor: a tentativa vira inconclusive (executor).
"""

import asyncio
import contextlib
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from lastro.config.loader import ProjectContext
from lastro.executors import get_executor
from lastro.history import (
    CreatedRun,
    ExecutorInfo,
    HistoryError,
    HistoryStore,
    JsonObject,
    RunStatus,
)

type Writer = Callable[[str], None]

# Folga além do teto da tentativa para o executor drenar e encerrar a run filha.
GRACE_SECONDS = 120
CLAUDE_PROMPT = (
    "Lastro reproduction attempt for run {run_id}. Call mission_context, follow "
    "skills/lastro/reproduce.md, report once with report_reproduction_result and call end_run."
)


class ExecutorUnavailable(Exception):
    """Binário, chave ou extra ausente; a mensagem traz a instrução de instalação."""


@dataclass(frozen=True)
class PreparedChild:
    executor: ExecutorInfo | None
    skill_bundle_version: str | None
    executor_snapshot: JsonObject = field(default_factory=dict[str, Any])
    handle: Any = None


@dataclass(frozen=True)
class LaunchOutcome:
    status: RunStatus | None
    reason: str | None
    available: bool = True
    note: str = ""


class ReproducerLauncher(Protocol):
    executor: str

    def prepare(self, project: ProjectContext) -> PreparedChild:
        """Confere instalação e credencial antes da run filha; falha com ExecutorUnavailable."""
        ...

    def launch(
        self,
        project: ProjectContext,
        history: HistoryStore,
        created: CreatedRun,
        prepared: PreparedChild,
    ) -> LaunchOutcome: ...


def unavailable_hint(executor: str, detail: str) -> str:
    spec = get_executor(executor)
    hint = spec.install_hint if spec is not None else ""
    return f"{executor} unavailable: {detail}" + (f" Install: {hint}" if hint else "")


def _outcome(history: HistoryStore, run_id: str) -> LaunchOutcome:
    record = history.get_run(run_id)
    return LaunchOutcome(status=record.status, reason=record.status_reason)


class NativeReproducer:
    executor = "native"

    def __init__(self, write: Writer) -> None:
        self.write = write

    def prepare(self, project: ProjectContext) -> PreparedChild:
        from lastro.native.runner import PreflightError, preflight

        try:
            prepared, provider = asyncio.run(preflight(project))
        except PreflightError as error:
            raise ExecutorUnavailable(unavailable_hint(self.executor, str(error))) from None
        return PreparedChild(
            executor=ExecutorInfo(name="native"),
            skill_bundle_version=prepared.bundle.sha256,
            handle=(prepared, provider),
        )

    def launch(
        self,
        project: ProjectContext,
        history: HistoryStore,
        created: CreatedRun,
        prepared: PreparedChild,
    ) -> LaunchOutcome:
        from lastro.native.runner import NativeRunner

        settings, provider = prepared.handle
        runner = NativeRunner(
            project,
            history,
            created.record.id,
            settings,
            provider,
            write=self.write,
            interactive=False,
        )
        asyncio.run(runner.run())
        return _outcome(history, created.record.id)


class CodexReproducer:
    executor = "codex"

    def __init__(self, write: Writer) -> None:
        self.write = write

    def prepare(self, project: ProjectContext) -> PreparedChild:
        from lastro.codex.launcher import executor_snapshot, prepare_codex
        from lastro.codex.preflight import CodexPreflightError

        try:
            prepared = prepare_codex(project)
        except CodexPreflightError as error:
            raise ExecutorUnavailable(unavailable_hint(self.executor, str(error))) from None
        return PreparedChild(
            executor=ExecutorInfo(name="codex", version=prepared.install.version),
            skill_bundle_version=prepared.bundle_sha256,
            executor_snapshot=executor_snapshot(prepared),
        )

    def launch(
        self,
        project: ProjectContext,
        history: HistoryStore,
        created: CreatedRun,
        prepared: PreparedChild,
    ) -> LaunchOutcome:
        from lastro.codex.launcher import LaunchRefused, launch
        from lastro.codex.models import LaunchMode
        from lastro.codex.preflight import CodexPreflightError

        try:
            launch(project, history, created.record.id, LaunchMode.HEADLESS, write=self.write)
        except (LaunchRefused, CodexPreflightError) as error:
            return LaunchOutcome(
                status=None,
                reason=None,
                available=False,
                note=unavailable_hint(self.executor, str(error)),
            )
        return _outcome(history, created.record.id)


class ClaudeReproducer:
    """`claude -p` headless com o mesmo .mcp.json e as permissões geradas pelo init."""

    executor = "claude-code"

    def __init__(self, write: Writer, run: Callable[..., Any] = subprocess.run) -> None:
        self.write = write
        self.run = run

    def prepare(self, project: ProjectContext) -> PreparedChild:
        from lastro.mission import BundleError, instruction_problems, verify_bundle

        spec = get_executor(self.executor)
        assert spec is not None and spec.external is not None
        binary = shutil.which(spec.external.binary)
        if binary is None:
            raise ExecutorUnavailable(unavailable_hint(self.executor, "claude not found in PATH."))
        project_dir = project.loaded.project_dir
        missing = [
            str(path)
            for path in (project_dir / ".mcp.json", project_dir / spec.external.permission_file)
            if not path.is_file()
        ]
        if missing:
            raise ExecutorUnavailable(
                unavailable_hint(self.executor, f"missing {', '.join(missing)}; run 'lastro init'.")
            )
        try:
            bundle = verify_bundle(project_dir)
        except BundleError as error:
            raise ExecutorUnavailable(unavailable_hint(self.executor, str(error))) from None
        problems = instruction_problems(project_dir / spec.external.instruction_file)
        if problems:
            raise ExecutorUnavailable(unavailable_hint(self.executor, "; ".join(problems)))
        return PreparedChild(
            executor=ExecutorInfo(name=self.executor),
            skill_bundle_version=bundle.sha256,
            handle=binary,
        )

    def launch(
        self,
        project: ProjectContext,
        history: HistoryStore,
        created: CreatedRun,
        prepared: PreparedChild,
    ) -> LaunchOutcome:
        run_id = created.record.id
        limits = project.profile.run_limits
        arguments = [
            str(prepared.handle),
            "-p",
            CLAUDE_PROMPT.format(run_id=run_id),
            "--output-format",
            "json",
            "--mcp-config",
            ".mcp.json",
            "--strict-mcp-config",
        ]
        log = history.run_dir(run_id) / "reproducer.log"
        try:
            with log.open("w", encoding="utf-8") as handle:
                completed = self.run(
                    arguments,
                    cwd=project.loaded.project_dir,
                    stdin=subprocess.DEVNULL,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    timeout=limits.max_minutes * 60 + GRACE_SECONDS,
                    check=False,
                )
        except (OSError, subprocess.SubprocessError) as error:
            return LaunchOutcome(
                status=None,
                reason=None,
                available=False,
                note=unavailable_hint(self.executor, f"{type(error).__name__}: {error}"),
            )
        returncode = getattr(completed, "returncode", 0)
        with contextlib.suppress(HistoryError):
            record = history.get_run(run_id)
            if not record.status.is_terminal and returncode != 0:
                return LaunchOutcome(
                    status=None,
                    reason=None,
                    available=False,
                    note=unavailable_hint(self.executor, f"claude exited with {returncode}."),
                )
        return _outcome(history, run_id)


def default_launchers(write: Writer) -> dict[str, ReproducerLauncher]:
    return {
        "native": NativeReproducer(write),
        "codex": CodexReproducer(write),
        "claude-code": ClaudeReproducer(write),
    }
