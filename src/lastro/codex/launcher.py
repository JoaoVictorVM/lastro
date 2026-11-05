"""Supervisor do Codex: revalida, reivindica a run, abre o filho e garante um único término."""

import collections
import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import IO, Any, cast

from dotenv import dotenv_values

from lastro.codex import config as codex_config
from lastro.codex.events import (
    MAX_LINE_BYTES,
    MCP_STARTUP_MARKER,
    REASON_EXITED,
    REASON_MCP,
    REASON_PROTOCOL,
    CodexProtocolError,
    Failure,
    classify_exec_event,
    classify_rollout_record,
    display_text,
    parse_line,
)
from lastro.codex.lifecycle import Closure, RunCloser, Writer
from lastro.codex.models import (
    MINIMUM_VERSION,
    CodexInstall,
    CodexResult,
    LaunchMode,
    LaunchPlan,
)
from lastro.codex.preflight import (
    CodexPreflightError,
    Runner,
    child_environment,
    codex_home,
    default_runner,
    probe_install,
)
from lastro.config.loader import ENV_FILENAME, ProjectContext
from lastro.history import HistoryError, HistoryStore, LaunchClaimError, RunRecord, RunStatus
from lastro.history.redaction import redact_text
from lastro.mission import BundleError, instruction_problems, verify_bundle
from lastro.skills import SKILL_FILES

CHILD_STOP_TIMEOUT_SECONDS = 10.0
POLL_SECONDS = 0.5
STDERR_TAIL_LINES = 20
PROMPT = (
    "Run the Lastro mission {run_id}. Call mission_context first and follow the Lastro "
    "instructions until end_run."
)
INSTRUCTIONS_HEADER = (
    "You are running a Lastro mission. Follow this verified skill bundle; only the Lastro MCP "
    "tools can operate the target.\n"
)
CANCEL_REASON = "user_cancelled"

type Spawn = Callable[..., subprocess.Popen[bytes]]

# Ponto de injeção dos testes: Codex simulado no lugar da instalação real.
PROBE: Callable[[], CodexInstall] = probe_install


class LaunchRefused(Exception):
    """Recusa antes de abrir o filho: a run segue como estava, saída 2."""


@dataclass(frozen=True)
class PreparedCodex:
    """Resultado do preflight do run start, sem efeito no histórico."""

    install: CodexInstall
    manifest_sha256: str
    bundle_sha256: str


def _check_credential_source(project: ProjectContext) -> None:
    """O filho não herda o ambiente; o MCP só alcança credenciais vindas do .env do projeto."""
    available = dotenv_values(project.loaded.project_dir / ENV_FILENAME)
    missing = [
        name for name in project.profile.credential_variable_names() if not available.get(name)
    ]
    if missing:
        raise CodexPreflightError(
            f"Codex runs read target credentials from {ENV_FILENAME}; define there: "
            + ", ".join(missing)
        )


def prepare_codex(
    project: ProjectContext, *, probe: Callable[[], CodexInstall] | None = None
) -> PreparedCodex:
    """Instalação, login, regras, instruções e bundle; falhar aqui não cria run."""
    project_dir = project.loaded.project_dir
    install = (probe or PROBE)()
    codex_config.load_policy(project_dir)
    manifest = codex_config.verify_manifest(project_dir)
    try:
        bundle = verify_bundle(project_dir)
    except BundleError as error:
        raise CodexPreflightError(str(error)) from None
    problems = instruction_problems(project_dir / codex_config.INSTRUCTIONS_FILE)
    if problems:
        raise CodexPreflightError(
            f"{'; '.join(problems)}; run 'lastro init --executor codex' and adjust the file."
        )
    _check_credential_source(project)
    return PreparedCodex(install, manifest, bundle.sha256)


def executor_snapshot(prepared: PreparedCodex) -> dict[str, Any]:
    return {
        "codex": {
            "auth_method": "chatgpt",
            "minimum_version": MINIMUM_VERSION,
            "sandbox_manifest_sha256": prepared.manifest_sha256,
            "launch_pending": True,
        }
    }


def _instructions(project_dir: Path) -> str:
    """O agente não tem ferramenta de arquivo: recebe o conteúdo já conferido pelo manifest."""
    sections = [INSTRUCTIONS_HEADER]
    for name in SKILL_FILES:
        text = (project_dir / "skills" / "lastro" / name).read_text(encoding="utf-8")
        sections.append(f'<lastro_skill file="{name}">\n{text}\n</lastro_skill>')
    return "\n".join(sections)


def _frozen_block(record: RunRecord) -> dict[str, Any]:
    block = record.config_snapshot.get("codex")
    return cast(dict[str, Any], block) if isinstance(block, dict) else {}


def revalidate(
    project: ProjectContext,
    history: HistoryStore,
    run_id: str,
    mode: LaunchMode,
    prepared: PreparedCodex,
) -> LaunchPlan:
    """Relê o registro e confronta o que foi congelado com o estado atual da máquina."""
    try:
        record = history.get_run(run_id)
    except HistoryError:
        raise LaunchRefused(f"Run {run_id} was not found in this project.") from None
    if record.project_id != project.require_project_id() or record.profile != project.profile_name:
        raise LaunchRefused(f"Run {run_id} belongs to another project or profile.")
    if record.executor_name != "codex":
        raise LaunchRefused(f"Run {run_id} uses executor {record.executor_name}, not codex.")
    if record.status.is_terminal:
        raise LaunchRefused(f"Run {run_id} already ended as {record.status.value}.")
    block = _frozen_block(record)
    if block.get("launch_pending") is not True:
        raise LaunchRefused(f"Run {run_id} was already launched; a second session is not allowed.")
    if record.executor_version != prepared.install.version:
        raise LaunchRefused(
            f"Codex changed since run start: recorded {record.executor_version}, "
            f"installed {prepared.install.version}. Start a new run."
        )
    if record.skill_bundle_version != prepared.bundle_sha256:
        raise LaunchRefused("The skill bundle changed since run start. Start a new run.")
    if block.get("sandbox_manifest_sha256") != prepared.manifest_sha256:
        raise LaunchRefused("The Codex rules changed since run start. Start a new run.")
    return LaunchPlan(
        executor="codex",
        executor_version=prepared.install.version,
        auth_method="chatgpt",
        mode=mode,
        run_id=run_id,
        profile=project.profile_name,
        project_dir=str(project.loaded.project_dir),
        config_file=str(project.config_path),
        skill_bundle_sha256=prepared.bundle_sha256,
    )


def build_arguments(install: CodexInstall, plan: LaunchPlan, overrides: list[str]) -> list[str]:
    """Lista fixa de argumentos: nada do usuário nem da missão é repassado à CLI."""
    prompt = PROMPT.format(run_id=plan.run_id)
    if plan.mode is LaunchMode.HEADLESS:
        return [
            *install.command,
            "exec",
            "--json",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--cd",
            plan.project_dir,
            *overrides,
            prompt,
        ]
    return [
        *install.command,
        "--no-daemon",
        "--no-alt-screen",
        "--cd",
        plan.project_dir,
        *overrides,
        prompt,
    ]


def stop_process_tree(child: subprocess.Popen[bytes], *, own_group: bool) -> None:
    """Fecha Codex, MCP e browser; no Windows só o taskkill alcança os descendentes."""
    if child.poll() is not None and not own_group:
        return
    if sys.platform == "win32":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/PID", str(child.pid), "/T", "/F"],
                capture_output=True,
                timeout=CHILD_STOP_TIMEOUT_SECONDS,
                check=False,
            )
    elif own_group:
        with contextlib.suppress(OSError):
            os.killpg(child.pid, signal.SIGTERM)
    else:
        with contextlib.suppress(OSError):
            child.terminate()
    try:
        child.wait(timeout=CHILD_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        if sys.platform != "win32" and own_group:
            with contextlib.suppress(OSError):
                os.killpg(child.pid, signal.SIGKILL)
        else:
            child.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            child.wait(timeout=CHILD_STOP_TIMEOUT_SECONDS)


class RolloutWatcher:
    """Canal da sessão interativa: o registro da própria CLI, sem raspar texto do terminal."""

    def __init__(self, home: Path, project_dir: Path, started: float) -> None:
        self.sessions = home / "sessions"
        self.project_dir = project_dir
        self.started = started
        self.path: Path | None = None
        self.offset = 0
        self.partial = b""

    def _candidates(self) -> Iterator[Path]:
        today = date.today()
        for day in (today, today - timedelta(days=1)):
            folder = self.sessions / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
            if folder.is_dir():
                yield from folder.glob("rollout-*.jsonl")

    def _belongs_here(self, path: Path) -> bool:
        with path.open("rb") as handle:
            first = handle.readline(MAX_LINE_BYTES)
        try:
            payload = parse_line(first).get("payload")
        except CodexProtocolError:
            return False
        cwd = cast(dict[str, Any], payload).get("cwd") if isinstance(payload, dict) else None
        return isinstance(cwd, str) and Path(cwd).resolve() == self.project_dir.resolve()

    def _locate(self) -> Path | None:
        with contextlib.suppress(OSError):
            # Sessão anterior no mesmo diretório nunca é tocada depois do início do filho.
            for path in sorted(self._candidates(), key=lambda item: item.stat().st_mtime):
                if path.stat().st_mtime >= self.started and self._belongs_here(path):
                    return path
        return None

    def poll(self) -> Failure | None:
        if self.path is None:
            self.path = self._locate()
            if self.path is None:
                return None
        try:
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                data = handle.read()
                self.offset = handle.tell()
        except OSError:
            return None
        lines = (self.partial + data).split(b"\n")
        self.partial = lines.pop()
        for line in lines:
            # Itens de resposta podem exceder o limite; não decidem término e são ignorados.
            with contextlib.suppress(CodexProtocolError):
                failure = classify_rollout_record(parse_line(line))
                if failure is not None:
                    return failure
        return None


class Supervisor:
    def __init__(
        self,
        project: ProjectContext,
        history: HistoryStore,
        plan: LaunchPlan,
        install: CodexInstall,
        arguments: list[str],
        *,
        write: Writer,
        spawn: Spawn = subprocess.Popen,
    ) -> None:
        self.plan, self.install, self.arguments = plan, install, arguments
        self.history = history
        self.write = write
        self.spawn = spawn
        self.closer = RunCloser(project, history, plan.run_id, write=write)
        self.child: subprocess.Popen[bytes] | None = None
        self.own_group = plan.mode is LaunchMode.HEADLESS and sys.platform != "win32"
        self.stderr_tail: collections.deque[str] = collections.deque(maxlen=STDERR_TAIL_LINES)

    def run(self) -> CodexResult:
        terminate = _install_termination_handler()
        failure: Failure | None = None
        try:
            try:
                self.child = self._open()
            except OSError:
                closure = self.closer.finish_locally(RunStatus.INTERRUPTED, REASON_EXITED)
                return self.closer.result(closure, self.install.version)
            if self.plan.mode is LaunchMode.HEADLESS:
                failure = self._follow_exec()
            else:
                failure = self._follow_interactive()
            closure = self._finish(failure)
        except KeyboardInterrupt:
            closure = self._cancel()
        finally:
            terminate()
        code = self.child.returncode if self.child is not None else None
        return self.closer.result(closure, self.install.version, code)

    def _open(self) -> subprocess.Popen[bytes]:
        headless = self.plan.mode is LaunchMode.HEADLESS
        child = self.spawn(
            self.arguments,
            cwd=self.plan.project_dir,
            env=child_environment(),
            stdin=subprocess.DEVNULL if headless else None,
            stdout=subprocess.PIPE if headless else None,
            stderr=subprocess.PIPE if headless else None,
            start_new_session=self.own_group,
        )
        if child.stderr is not None:
            threading.Thread(target=self._drain_stderr, args=(child.stderr,), daemon=True).start()
        return child

    def _drain_stderr(self, stream: IO[bytes]) -> None:
        for raw in stream:
            self.stderr_tail.append(redact_text(raw[:2000].decode("utf-8", "replace")).rstrip())

    def _follow_exec(self) -> Failure | None:
        assert self.child is not None and self.child.stdout is not None
        while True:
            line = self.child.stdout.readline(MAX_LINE_BYTES + 1)
            if not line:
                self.child.wait()
                return None
            try:
                if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
                    raise CodexProtocolError("truncated or oversized line")
                event = parse_line(line)
            except CodexProtocolError:
                self.write("! Codex emitted an invalid event; stopping the session.")
                return Failure(REASON_PROTOCOL)
            text = display_text(event)
            if text:
                self.write(text)
            failure = classify_exec_event(event)
            if failure is not None:
                return failure

    def _follow_interactive(self) -> Failure | None:
        assert self.child is not None
        watcher = RolloutWatcher(
            codex_home(), Path(self.plan.project_dir), time.time() - POLL_SECONDS
        )
        while self.child.poll() is None:
            failure = watcher.poll()
            if failure is not None:
                return failure
            time.sleep(POLL_SECONDS)
        # Último evento gravado antes da saída também conta.
        return watcher.poll()

    def _classify_exit(self, failure: Failure | None) -> str:
        if failure is not None:
            return failure.reason
        if any(MCP_STARTUP_MARKER in line for line in self.stderr_tail):
            return REASON_MCP
        return REASON_EXITED

    def _finish(self, failure: Failure | None) -> Closure:
        reason = self._classify_exit(failure)
        return self._close(RunStatus.INTERRUPTED, reason)

    def _cancel(self) -> Closure:
        try:
            return self._close(RunStatus.CANCELLED, CANCEL_REASON)
        except KeyboardInterrupt:
            self._stop_child()
            return self.closer.finish_locally(RunStatus.CANCELLED, CANCEL_REASON)

    def _stop_child(self) -> None:
        if self.child is not None:
            stop_process_tree(self.child, own_group=self.own_group)

    def _close(self, status: RunStatus, reason: str) -> Closure:
        """Término já gravado prevalece; senão, lock pelo owner antes de encerrar o histórico."""
        record = self.history.get_run(self.plan.run_id)
        closure: Closure | None = None
        child_alive = self.child is not None and self.child.poll() is None
        if not record.status.is_terminal and child_alive:
            closure = self.closer.request_owner_end(status, reason)
        self._stop_child()
        if closure is None:
            self.closer.wait_owner_gone()
            closure = self.closer.finish_locally(status, reason)
        self.closer.clear_owner_files()
        return closure


def _install_termination_handler() -> Callable[[], None]:
    """SIGTERM e Ctrl+Break seguem o mesmo encerramento do Ctrl+C."""

    def interrupt(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    names = ("SIGTERM", "SIGBREAK")
    numbers = [getattr(signal, name) for name in names if hasattr(signal, name)]
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    previous = {number: signal.signal(number, interrupt) for number in numbers}

    def restore() -> None:
        for number, handler in previous.items():
            signal.signal(number, handler)

    return restore


def launch(
    project: ProjectContext,
    history: HistoryStore,
    run_id: str,
    mode: LaunchMode,
    *,
    write: Writer,
    probe: Callable[[], CodexInstall] | None = None,
    runner: Runner = default_runner,
    spawn: Spawn = subprocess.Popen,
) -> CodexResult:
    """Interface usada pela CLI e pelo futuro runner de avaliação; não recria a run."""
    prepared = prepare_codex(project, probe=probe)
    plan = revalidate(project, history, run_id, mode, prepared)
    project_dir = project.loaded.project_dir
    home = codex_home()
    problems = codex_config.user_config_problems(home)
    if problems and mode is LaunchMode.INTERACTIVE:
        raise codex_config.SandboxRulesError(
            f"{codex_config.INVALID_MESSAGE}: {'; '.join(problems)}"
        )
    policy = codex_config.load_policy(project_dir)
    identity = codex_config.SessionIdentity(
        project_dir=project_dir,
        config_path=project.config_path,
        profile=project.profile_name,
        run_id=run_id,
        private_root=project.loaded.private_data_root,
        codex_home=home,
        instructions=_instructions(project_dir),
    )
    try:
        foreign = codex_config.resolve_foreign_servers(
            prepared.install.command, policy, identity, runner
        )
        overrides = codex_config.session_overrides(policy, identity, foreign)
        codex_config.verify_effective_tools(prepared.install.command, overrides, runner)
        codex_config.sandbox_self_test(prepared.install.command, overrides, identity, runner)
        if mode is LaunchMode.HEADLESS:
            # O exec ignora a configuração do usuário; desligar um servidor que ele não carrega
            # criaria uma entrada sem comando, que a CLI rejeitaria.
            overrides = codex_config.session_overrides(policy, identity, [])
    except (OSError, subprocess.SubprocessError) as error:
        raise codex_config.SandboxRulesError(
            f"{codex_config.INVALID_MESSAGE}: verification failed ({type(error).__name__})"
        ) from None
    arguments = build_arguments(prepared.install, plan, overrides)
    try:
        history.claim_launch(run_id)
    except LaunchClaimError as error:
        raise LaunchRefused(str(error)) from None
    return Supervisor(
        project, history, plan, prepared.install, arguments, write=write, spawn=spawn
    ).run()


def result_json(result: CodexResult) -> str:
    return json.dumps(result.document(), ensure_ascii=False, sort_keys=True)
