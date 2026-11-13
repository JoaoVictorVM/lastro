"""Stack Langfuse local: materialização privada, preflight do Docker e subida idempotente.

Só infraestrutura: não cria usuário, projeto nem chaves, e não altera perfis. Docker roda por
lista de argumentos, sem shell e sem janela; a saída dele vai para um diagnóstico privado e
redigido, nunca para o terminal. Segredos e volumes existentes são sempre reutilizados.
"""

import contextlib
import hashlib
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from lastro.history.redaction import redact_text

DEFAULT_PORT = 3000
READINESS_SECONDS = 120.0
COMPOSE_TIMEOUT_SECONDS = 1800.0
STACK_SUBDIR = Path("observability") / "langfuse"
COMPOSE_FILENAME = "compose.yaml"
ENV_FILENAME = "langfuse.env"
DIAGNOSTIC_FILENAME = "docker.log"
HEALTH_PATH = "/api/public/health"
POSIX_FILE_MODE = 0o600
_SECRET_VALUE = re.compile(r"^[A-Za-z0-9]{16,128}$")
_SECRET_NAMES = (
    "NEXTAUTH_SECRET",
    "SALT",
    "ENCRYPTION_KEY",
    "POSTGRES_PASSWORD",
    "CLICKHOUSE_PASSWORD",
    "REDIS_AUTH",
    "MINIO_ROOT_PASSWORD",
)

type Runner = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]
type Probe = Callable[[str], bool]


class LocalStackError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class LocalStack:
    directory: Path
    project_name: str
    port: int

    @property
    def url(self) -> str:
        return f"http://localhost:{self.port}"

    @property
    def compose_path(self) -> Path:
        return self.directory / COMPOSE_FILENAME

    @property
    def env_path(self) -> Path:
        return self.directory / ENV_FILENAME

    def compose(self, *arguments: str) -> list[str]:
        return [
            "docker",
            "compose",
            "--project-name",
            self.project_name,
            "--file",
            str(self.compose_path),
            "--env-file",
            str(self.env_path),
            *arguments,
        ]


def stack_for(project_data_dir: Path, project_id: str, port: int = DEFAULT_PORT) -> LocalStack:
    """Nome Compose derivado só do project id; nada da missão, do shell ou do agente."""
    digest = hashlib.sha256(f"lastro/langfuse/{project_id}".encode()).hexdigest()[:12]
    return LocalStack(project_data_dir / STACK_SUBDIR, f"lastro-langfuse-{digest}", port)


def bundled_compose() -> bytes:
    return (
        resources.files("lastro.observability").joinpath("resources", COMPOSE_FILENAME).read_bytes()
    )


def _protect(path: Path) -> None:
    if sys.platform == "win32":
        from lastro.auth import winacl

        winacl.apply_owner_only(path, directory=False)
        if not winacl.is_owner_only(path):
            raise LocalStackError("obs_config_invalid", "could not protect the stack env file")
        return
    os.chmod(path, POSIX_FILE_MODE)
    if stat.S_IMODE(path.stat().st_mode) != POSIX_FILE_MODE:
        raise LocalStackError("obs_config_invalid", "could not protect the stack env file")


def _create_private(path: Path, content: str) -> None:
    if sys.platform == "win32":
        from lastro.auth import winacl

        winacl.create_owner_only_file(path)
        path.write_text(content, encoding="utf-8", newline="\n")
    else:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, POSIX_FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
    _protect(path)


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _env_content(values: dict[str, str]) -> str:
    lines = ["# Segredos locais da stack Langfuse do Lastro; gerados uma vez, nunca versionar."]
    lines += [f"{key}={value}" for key, value in values.items()]
    return "\n".join(lines) + "\n"


def materialize(stack: LocalStack) -> None:
    """Escreve o Compose empacotado e cria os segredos só na primeira vez."""
    stack.directory.mkdir(parents=True, exist_ok=True)
    stack.compose_path.write_bytes(bundled_compose())
    if stack.env_path.exists():
        values = read_env(stack.env_path)
        _validate(values, stack)
        _protect(stack.env_path)
        return
    values = {
        "LASTRO_LANGFUSE_PORT": str(stack.port),
        "NEXTAUTH_URL": stack.url,
        **{name: secrets.token_hex(32) for name in _SECRET_NAMES},
    }
    _create_private(stack.env_path, _env_content(values))


def _validate(values: dict[str, str], stack: LocalStack) -> None:
    """Valores entram na interpolação do Compose: só alfanuméricos, sem `$` nem aspas."""
    invalid = [name for name in _SECRET_NAMES if not _SECRET_VALUE.match(values.get(name, ""))]
    if invalid:
        raise LocalStackError(
            "obs_config_invalid",
            f"invalid values in {stack.env_path.name}: {', '.join(invalid)} "
            "(expected 16 to 128 letters or digits)",
        )
    port = values.get("LASTRO_LANGFUSE_PORT", "")
    if not port.isdigit() or not 1024 <= int(port) <= 65535:
        raise LocalStackError("obs_config_invalid", "invalid LASTRO_LANGFUSE_PORT")


def stack_port(stack: LocalStack) -> LocalStack:
    """A porta efetiva é a já gravada no arquivo privado, para reutilizar a mesma stack."""
    if not stack.env_path.exists():
        return stack
    port = read_env(stack.env_path).get("LASTRO_LANGFUSE_PORT", "")
    return LocalStack(stack.directory, stack.project_name, int(port)) if port.isdigit() else stack


def default_runner(arguments: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    return subprocess.run(
        list(arguments),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL,
        creationflags=flags,
    )


def default_probe(url: str) -> bool:
    import httpx

    try:
        response = httpx.get(url, timeout=2.0, follow_redirects=False, trust_env=False)
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


class LocalStackManager:
    def __init__(
        self,
        stack: LocalStack,
        *,
        runner: Runner | None = None,
        probe: Probe | None = None,
        port_check: Callable[[int], bool] | None = None,
        progress: Callable[[str], None] = lambda _: None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        readiness_seconds: float | None = None,
    ) -> None:
        self.stack = stack_port(stack)
        self._runner = runner or default_runner
        self._probe = probe or default_probe
        self._port_check = port_check or port_in_use
        self._progress = progress
        self._monotonic = monotonic
        self._sleep = sleep
        self._readiness = READINESS_SECONDS if readiness_seconds is None else readiness_seconds

    def _run(self, arguments: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        try:
            completed = self._runner(arguments, timeout)
        except FileNotFoundError:
            raise LocalStackError("docker_unavailable", "Docker was not found in PATH.") from None
        except subprocess.TimeoutExpired:
            raise LocalStackError("obs_start_timeout", "Docker did not answer in time.") from None
        self._diagnose(arguments, completed)
        return completed

    def _diagnose(
        self, arguments: Sequence[str], completed: subprocess.CompletedProcess[str]
    ) -> None:
        if not self.stack.directory.exists():
            return
        text = redact_text(
            f"$ {' '.join(arguments[:2])} ... -> {completed.returncode}\n"
            f"{completed.stdout or ''}{completed.stderr or ''}\n"
        )
        with (
            contextlib.suppress(OSError),
            (self.stack.directory / DIAGNOSTIC_FILENAME).open("a", encoding="utf-8") as handle,
        ):
            handle.write(text)

    def preflight(self) -> None:
        if self._run(["docker", "version", "--format", "{{.Server.Version}}"], 30).returncode:
            raise LocalStackError("docker_unavailable", "Docker is not running or not reachable.")
        if self._run(["docker", "compose", "version", "--short"], 30).returncode:
            raise LocalStackError("compose_unavailable", "Docker Compose v2 is not available.")

    def _running(self) -> bool:
        completed = self._run(
            self.stack.compose("ps", "--status", "running", "-q", "langfuse-web"), 60
        )
        return completed.returncode == 0 and bool(completed.stdout.strip())

    def up(self) -> str:
        """Sobe a stack e espera a UI responder; timeout não remove volumes nem segredos."""
        self.preflight()
        materialize(self.stack)
        self.stack = stack_port(self.stack)
        if not self._running() and self._port_check(self.stack.port):
            raise LocalStackError(
                "obs_port_in_use",
                f"Port {self.stack.port} on 127.0.0.1 is already in use by another process.",
            )
        self._progress("Starting the local Langfuse stack (first run downloads the images)...")
        started = self._run(self.stack.compose("up", "--detach"), COMPOSE_TIMEOUT_SECONDS)
        if started.returncode:
            raise LocalStackError(
                "docker_unavailable",
                f"docker compose up failed; see {self.stack.directory / DIAGNOSTIC_FILENAME}",
            )
        deadline = self._monotonic() + self._readiness
        health = self.stack.url + HEALTH_PATH
        while self._monotonic() < deadline:
            if self._probe(health):
                return self.stack.url
            self._progress("Waiting for Langfuse to become ready...")
            self._sleep(2.0)
        raise LocalStackError(
            "obs_start_timeout",
            f"Langfuse did not become ready in {int(self._readiness)} seconds; the stack and its "
            "data were kept. Run 'lastro obs up' again to keep waiting.",
        )
