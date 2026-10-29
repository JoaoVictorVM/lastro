"""Ciclo de vida do servidor do demo: iniciar, parar e consultar estado.

Interface consumida pela CLI (`lastro demo up` e `lastro demo down`). O servidor roda como
processo desacoplado do mesmo interpretador; pid, porta e url ficam num arquivo de estado dentro
de `state_dir`, junto com o banco, o log de acesso e o log do servidor.
"""

import json
import os
import socket
import sys
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx

from lastro_demo import DEFAULT_HOST, DEFAULT_PORT, _process
from lastro_demo.config import (
    ACCESS_LOG_FILENAME,
    DB_FILENAME,
    SERVER_LOG_FILENAME,
    SERVER_PID_FILENAME,
    SHUTDOWN_FILENAME,
    STATE_FILENAME,
    announced_credential_env_vars,
)
from lastro_demo.db import count_rows
from lastro_demo.flags import validate_defects, validate_scenarios, validate_variant

READY_TIMEOUT_SECONDS = 30.0
GRACEFUL_STOP_SECONDS = 10.0
FORCED_STOP_SECONDS = 5.0
PROBE_TIMEOUT_SECONDS = 1.0
POLL_SECONDS = 0.1

READY_TEMPLATE = "Demo ready: {clinics} clinics, {employees} employees, {defects} defects active"


class DemoRunnerError(RuntimeError):
    pass


class PortInUseError(DemoRunnerError):
    pass


class DemoNotReadyError(DemoRunnerError):
    pass


class StateDirNotWritableError(DemoRunnerError):
    pass


class DemoStopError(DemoRunnerError):
    pass


@dataclass(frozen=True)
class DemoState:
    url: str
    host: str
    port: int
    pid: int
    state_dir: Path
    defects: tuple[str, ...]
    scenarios: tuple[str, ...]
    variant: str
    clinics: int
    employees: int
    credential_env_vars: tuple[str, ...]
    ready_message: str
    access_log_path: Path
    server_log_path: Path
    started_at: str

    @property
    def flags(self) -> tuple[str, ...]:
        return self.defects + self.scenarios


@dataclass(frozen=True)
class StopResult:
    was_running: bool
    message: str | None = None


@dataclass(frozen=True)
class DemoStatus:
    state: Literal["running", "stopped", "stale"]
    url: str | None = None
    pid: int | None = None
    defects: tuple[str, ...] = ()
    scenarios: tuple[str, ...] = ()
    variant: str | None = None


def ready_message(clinics: int, employees: int, defects: int) -> str:
    return READY_TEMPLATE.format(clinics=clinics, employees=employees, defects=defects)


def _state_file(state_dir: Path) -> Path:
    return state_dir / STATE_FILENAME


def _read_record(state_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(_state_file(state_dir).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        _clear_record(state_dir)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("pid"), int):
        _clear_record(state_dir)
        return None
    record: dict[str, Any] = data
    return record


def _write_record(state_dir: Path, record: dict[str, Any]) -> None:
    temporary = state_dir / f".{STATE_FILENAME}.{uuid.uuid4().hex}"
    temporary.write_text(json.dumps(record, indent=2), encoding="utf-8")
    os.replace(temporary, _state_file(state_dir))


def _clear_record(state_dir: Path) -> None:
    _state_file(state_dir).unlink(missing_ok=True)


def _probe_version(url: str) -> dict[str, Any] | None:
    try:
        with httpx.Client(trust_env=False, timeout=PROBE_TIMEOUT_SECONDS) as client:
            response = client.get(f"{url}/__demo/version")
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _live_server(
    state_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]] | Literal["stale"] | None:
    """Registro e versão do servidor vivo; "stale" quando o registro existe mas não responde."""
    record = _read_record(state_dir)
    if record is None:
        return None
    version = None
    if _process.pid_alive(int(record["pid"])):
        version = _probe_version(str(record.get("url", "")))
    if version is None:
        _clear_record(state_dir)
        return "stale"
    return record, version


def _ensure_writable(state_dir: Path) -> None:
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        probe = state_dir / f".write-test-{uuid.uuid4().hex}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise StateDirNotWritableError(
            f"Sem permissão de escrita na pasta de estado do demo {state_dir}: "
            f"{exc.strerror or exc}"
        ) from exc


def _port_in_use(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.settimeout(PROBE_TIMEOUT_SECONDS)
        if probe.connect_ex((host, port)) == 0:
            return True
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return True
    return False


def _flag_argument(values: Iterable[str]) -> str:
    return ",".join(sorted(values)) or "none"


def _server_argv(
    host: str,
    port: int,
    state_dir: Path,
    defects: Iterable[str] | None,
    scenarios: Iterable[str] | None,
    variant: str | None,
) -> list[str]:
    # -P tira a pasta atual do sys.path: o pacote vem sempre do ambiente instalado.
    argv = [sys.executable, "-P", "-m", "lastro_demo"]
    argv += ["--host", host, "--port", str(port), "--state-dir", str(state_dir)]
    if defects is not None:
        argv += ["--defects", _flag_argument(validate_defects(defects))]
    if scenarios is not None:
        argv += ["--scenarios", _flag_argument(validate_scenarios(scenarios))]
    if variant is not None:
        argv += ["--variant", validate_variant(variant)]
    return argv


def _url(host: str, port: int) -> str:
    shown = f"[{host}]" if ":" in host else host
    return f"http://{shown}:{port}"


def _wait_ready(url: str, pid: int, server_log: Path, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        version = _probe_version(url)
        if version is not None:
            return version
        if not _process.pid_alive(pid):
            raise DemoNotReadyError(
                f"O servidor do demo encerrou antes de ficar pronto. Veja o log em {server_log}."
            )
        time.sleep(POLL_SECONDS)
    raise DemoNotReadyError(
        f"O servidor do demo não ficou pronto em {timeout:.0f} s. Veja o log em {server_log}."
    )


def _build_state(state_dir: Path, record: dict[str, Any], version: dict[str, Any]) -> DemoState:
    clinics, employees = count_rows(state_dir / DB_FILENAME)
    defects = tuple(str(item) for item in version.get("defects", []))
    scenarios = tuple(str(item) for item in version.get("scenarios", []))
    return DemoState(
        url=str(record["url"]),
        host=str(record["host"]),
        port=int(record["port"]),
        pid=int(record["pid"]),
        state_dir=state_dir,
        defects=defects,
        scenarios=scenarios,
        variant=str(version.get("variant", "base")),
        clinics=clinics,
        employees=employees,
        credential_env_vars=announced_credential_env_vars(),
        ready_message=ready_message(clinics, employees, len(defects)),
        access_log_path=state_dir / ACCESS_LOG_FILENAME,
        server_log_path=state_dir / SERVER_LOG_FILENAME,
        started_at=str(record.get("started_at", "")),
    )


def _record_pids(record: dict[str, Any]) -> list[int]:
    pids = [int(record["pid"])]
    launcher = record.get("launcher_pid")
    if isinstance(launcher, int) and launcher not in pids:
        pids.append(launcher)
    return pids


def _force_stop(pids: list[int]) -> bool:
    # O lançador vem por último na lista e é encerrado primeiro, levando junto o filho.
    for pid in reversed(pids):
        _process.force_kill(pid)
    return all(_process.wait_exit(pid, FORCED_STOP_SECONDS) for pid in pids)


def _stop_process(state_dir: Path, record: dict[str, Any]) -> None:
    pids = _record_pids(record)
    shutdown_file = state_dir / SHUTDOWN_FILENAME
    shutdown_file.write_text("stop", encoding="utf-8")
    try:
        if all(_process.wait_exit(pid, GRACEFUL_STOP_SECONDS) for pid in pids):
            return
        if not _force_stop(pids):
            raise DemoStopError(
                f"Não foi possível encerrar o processo do demo (pid {', '.join(map(str, pids))})."
            )
    finally:
        shutdown_file.unlink(missing_ok=True)


def _server_pid(state_dir: Path, fallback: int) -> int:
    # No Windows o python.exe de um venv é um lançador que cria o interpretador real como
    # filho; o servidor grava o próprio pid para que a parada forçada atinja o processo certo.
    try:
        return int((state_dir / SERVER_PID_FILENAME).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return fallback


def start(
    *,
    state_dir: Path,
    port: int = DEFAULT_PORT,
    host: str = DEFAULT_HOST,
    defects: Iterable[str] | None = None,
    scenarios: Iterable[str] | None = None,
    variant: str | None = None,
    ready_timeout: float = READY_TIMEOUT_SECONDS,
) -> DemoState:
    """Sobe o servidor desacoplado e espera a prontidão; idempotente para o mesmo state_dir.

    Sem `defects`, `scenarios` ou `variant`, vale o ambiente do processo (padrão: tudo ativo).
    """
    state_dir = Path(state_dir)
    _ensure_writable(state_dir)
    live = _live_server(state_dir)
    if isinstance(live, tuple):
        record, version = live
        return _build_state(state_dir, record, version)
    argv = _server_argv(host, port, state_dir, defects, scenarios, variant)
    if _port_in_use(host, port):
        raise PortInUseError(
            f"A porta {port} já está em uso em {host}. Encerre o processo que a ocupa ou escolha "
            "outra porta com --port (ou demo.port no lastro.toml)."
        )
    (state_dir / SHUTDOWN_FILENAME).unlink(missing_ok=True)
    (state_dir / SERVER_PID_FILENAME).unlink(missing_ok=True)
    server_log = state_dir / SERVER_LOG_FILENAME
    url = _url(host, port)
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    with server_log.open("ab") as log:
        process = _process.spawn_detached(argv, cwd=state_dir, log=log, env=env)
    # Sem isso o objeto Popen avisaria, ao ser coletado, que o filho ainda está rodando.
    process.returncode = 0
    record: dict[str, Any] = {
        "pid": process.pid,
        "launcher_pid": process.pid,
        "host": host,
        "port": port,
        "url": url,
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "access_log": str(state_dir / ACCESS_LOG_FILENAME),
        "server_log": str(server_log),
    }
    _write_record(state_dir, record)
    try:
        version = _wait_ready(url, process.pid, server_log, ready_timeout)
    except DemoNotReadyError:
        server_pid = _server_pid(state_dir, process.pid)
        _force_stop([server_pid] if server_pid == process.pid else [server_pid, process.pid])
        _clear_record(state_dir)
        raise
    record["pid"] = _server_pid(state_dir, process.pid)
    _write_record(state_dir, record)
    return _build_state(state_dir, record, version)


def stop(*, state_dir: Path) -> StopResult:
    """Para o servidor (gracioso, depois forçado). Parar sem servidor rodando não é erro."""
    state_dir = Path(state_dir)
    live = _live_server(state_dir)
    if live is None:
        return StopResult(was_running=False)
    if live == "stale":
        return StopResult(
            was_running=False,
            message="O demo não estava rodando; o estado obsoleto foi descartado.",
        )
    record, _ = live
    _stop_process(state_dir, record)
    _clear_record(state_dir)
    return StopResult(was_running=True, message="Demo parado.")


def status(*, state_dir: Path) -> DemoStatus:
    live = _live_server(Path(state_dir))
    if live is None:
        return DemoStatus(state="stopped")
    if live == "stale":
        return DemoStatus(state="stale")
    record, version = live
    return DemoStatus(
        state="running",
        url=str(record["url"]),
        pid=int(record["pid"]),
        defects=tuple(str(item) for item in version.get("defects", [])),
        scenarios=tuple(str(item) for item in version.get("scenarios", [])),
        variant=str(version.get("variant", "base")),
    )
