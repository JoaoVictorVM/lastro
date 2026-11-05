"""Descobre executável, versão, login e capacidades sem abrir sessão de inferência."""

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from lastro.codex.models import MINIMUM_VERSION, CodexInstall, parse_version

PROBE_TIMEOUT_SECONDS = 60.0
INSTALL_HINT = "Install it with: npm install -g @openai/codex"
LOGIN_HINT = "Run 'codex login' and sign in with ChatGPT."
VERSION_LINE = re.compile(r"codex-cli (\S+)")
CHATGPT_LOGIN = "Logged in using ChatGPT"
# Sem essas opções o adapter não consegue fixar a sessão nem isolar o headless.
INTERACTIVE_FLAGS = ("--no-daemon", "--config")
EXEC_FLAGS = ("--json", "--ignore-user-config", "--ignore-rules", "--config")
SANDBOX_FLAGS = ("--permission-profile",)
# Ambiente mínimo: credenciais do alvo e chaves de API ficam fora do processo do Codex.
CHILD_ENV_ALLOWED = frozenset(
    {
        "APPDATA",
        "CODEX_HOME",
        "COLORTERM",
        "COMSPEC",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "LOCALAPPDATA",
        "LOGNAME",
        "NODE_EXTRA_CA_CERTS",
        "NO_PROXY",
        "PATH",
        "PATHEXT",
        "PLAYWRIGHT_BROWSERS_PATH",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TERM",
        "TMP",
        "TMPDIR",
        "USER",
        "USERDOMAIN",
        "USERNAME",
        "USERPROFILE",
        "WINDIR",
        "WT_SESSION",
    }
)
CHILD_ENV_PREFIXES = ("LC_", "XDG_")
PROXY_VARIABLES = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"})

type Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]
type Which = Callable[[str], str | None]


class CodexPreflightError(Exception):
    """Falha antes de criar ou lançar a run: nenhum contato com o alvo, saída 2."""


def child_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if environ is None else environ
    kept: dict[str, str] = {}
    for key, value in source.items():
        name = key.upper()
        if name in CHILD_ENV_ALLOWED or name.startswith(CHILD_ENV_PREFIXES):
            kept[key] = value
        elif name in PROXY_VARIABLES and "@" not in value:
            # Proxy com usuário e senha na URL não entra no contexto do agente.
            kept[key] = value
    return kept


def codex_home(environ: Mapping[str, str] | None = None) -> Path:
    source = os.environ if environ is None else environ
    configured = source.get("CODEX_HOME")
    return Path(configured) if configured else Path.home() / ".codex"


def default_runner(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=PROBE_TIMEOUT_SECONDS,
        check=False,
        stdin=subprocess.DEVNULL,
        env=child_environment(),
    )


def resolve_command(found: str, which: Which = shutil.which) -> tuple[str, ...]:
    """Executável real, sem cmd.exe: um .cmd reinterpretaria os argumentos da chamada."""
    path = Path(found).resolve()
    if path.suffix.lower() not in {".cmd", ".bat", ".ps1"}:
        return (str(path),)
    script = path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    local_node = path.parent / "node.exe"
    node = str(local_node) if local_node.is_file() else which("node")
    if not script.is_file() or node is None:
        raise CodexPreflightError(
            f"Codex launcher at {path} is not a supported installation. {INSTALL_HINT}"
        )
    return (str(Path(node).resolve()), str(script))


def _probe(runner: Runner, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return runner(arguments)
    except (OSError, subprocess.SubprocessError) as error:
        raise CodexPreflightError(
            f"Codex CLI could not be executed ({type(error).__name__}). {INSTALL_HINT}"
        ) from None


def check_version(command: tuple[str, ...], runner: Runner) -> str:
    completed = _probe(runner, [*command, "--version"])
    match = VERSION_LINE.search(completed.stdout)
    detected = match[1] if match else "unknown"
    parsed = parse_version(detected)
    minimum = parse_version(MINIMUM_VERSION)
    assert minimum is not None
    if completed.returncode != 0 or parsed is None:
        raise CodexPreflightError(
            f"Codex CLI version could not be validated: installed: {detected}; "
            f"required: >={MINIMUM_VERSION}"
        )
    if parsed < minimum:
        raise CodexPreflightError(
            f"Codex CLI is too old: installed: {detected}; required: >={MINIMUM_VERSION}"
        )
    return detected


def check_capabilities(command: tuple[str, ...], runner: Runner) -> None:
    for arguments, flags in (
        (["--help"], INTERACTIVE_FLAGS),
        (["exec", "--help"], EXEC_FLAGS),
        (["sandbox", "--help"], SANDBOX_FLAGS),
    ):
        completed = _probe(runner, [*command, *arguments])
        missing = [flag for flag in flags if flag not in completed.stdout]
        if completed.returncode != 0 or missing:
            label = " ".join(arguments[:-1]) or "codex"
            detail = ", ".join(missing) or "help unavailable"
            raise CodexPreflightError(f"Codex CLI lacks a required capability ({label}: {detail}).")


def check_login(command: tuple[str, ...], runner: Runner) -> None:
    """Só o relato da própria CLI: auth.json não é lido e o login não é aberto daqui."""
    completed = _probe(runner, [*command, "login", "status"])
    lines = (completed.stdout + "\n" + completed.stderr).splitlines()
    if completed.returncode == 0 and CHATGPT_LOGIN in (line.strip() for line in lines):
        return
    if any("api key" in line.lower() for line in lines):
        raise CodexPreflightError(f"Codex is logged in with an API key. {LOGIN_HINT}")
    raise CodexPreflightError(f"Codex has no ChatGPT login. {LOGIN_HINT}")


def probe_install(*, which: Which = shutil.which, runner: Runner = default_runner) -> CodexInstall:
    found = which("codex")
    if found is None:
        raise CodexPreflightError(
            f"Codex CLI not found: installed: not found; required: >={MINIMUM_VERSION}. "
            f"{INSTALL_HINT}"
        )
    command = resolve_command(found, which)
    version = check_version(command, runner)
    check_capabilities(command, runner)
    check_login(command, runner)
    return CodexInstall(command=command, version=version)
