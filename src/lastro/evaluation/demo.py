"""Demo gerenciado pela avaliação: instância própria, reset, versão e janelas do access log.

O supervisor abre o demo numa pasta de estado privada da suite e numa porta livre. Um demo
iniciado pelo usuário nunca é resetado: a exclusividade vem do arquivo de posse na pasta.
"""

import contextlib
import hashlib
import json
import os
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from lastro.evaluation.models import DemoSpec, EvalError

OWNER_FILE = "eval-owner.json"
ACCESS_LOG = "access.log"
CONTROL_TIMEOUT = 15.0
SEED_ENV = "LASTRO_DEMO_SEED"


@dataclass(frozen=True)
class ResetResult:
    ok: bool
    payload: dict[str, Any]
    error: str | None = None


@dataclass(frozen=True)
class LogWindow:
    start: int
    end: int
    sha256: str
    entries: tuple[dict[str, Any], ...]
    intact: bool


class DemoPort(Protocol):
    url: str

    def ensure(self, seed: int) -> str: ...

    def reset(self, demo: DemoSpec) -> ResetResult: ...

    def version(self) -> dict[str, Any]: ...

    def log_offset(self) -> int: ...

    def window(self, start: int, end: int) -> LogWindow: ...

    def stop(self) -> None: ...


def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def expected_flags(demo: DemoSpec) -> dict[str, Any]:
    return {
        "defects": sorted(demo.defects),
        "scenarios": sorted(demo.scenarios),
        "variant": demo.variant,
    }


def check_version(version: Mapping[str, Any], demo: DemoSpec, package_version: str) -> None:
    """Versão do pacote e flags efetivas iguais ao pedido; senão o trial é inválido."""
    if version.get("version") != package_version:
        raise EvalError("version_mismatch", f"demo version {version.get('version')!r}")
    actual = {
        "defects": sorted(version.get("defects", [])),
        "scenarios": sorted(version.get("scenarios", [])),
        "variant": version.get("variant"),
    }
    if actual != expected_flags(demo):
        raise EvalError("version_mismatch", "demo flags differ from the scenario")


def read_window(path: Path, start: int, end: int) -> LogWindow:
    """Trecho [start, end) do log; linha corrompida torna a janela não íntegra."""
    try:
        with path.open("rb") as handle:
            handle.seek(start)
            data = handle.read(max(0, end - start))
    except OSError:
        return LogWindow(start, end, hashlib.sha256(b"").hexdigest(), (), intact=False)
    intact = len(data) == end - start
    entries: list[dict[str, Any]] = []
    for line in data.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            intact = False
            continue
        if isinstance(item, dict):
            entries.append(item)  # pyright: ignore[reportUnknownArgumentType]
        else:
            intact = False
    return LogWindow(start, end, hashlib.sha256(data).hexdigest(), tuple(entries), intact)


class ManagedDemo:
    """Processo do demo pertencente à suite; o processo do demo é o único escritor do banco."""

    def __init__(
        self,
        state_dir: Path,
        owner_token: str,
        *,
        host: str = "127.0.0.1",
        client: Callable[[], httpx.Client] | None = None,
        package_version: str | None = None,
    ) -> None:
        self.state_dir = state_dir
        self.owner_token = owner_token
        self.host = host
        self.url = ""
        self.seed: int | None = None
        self._client = client or (lambda: httpx.Client(timeout=CONTROL_TIMEOUT))
        if package_version is None:
            from lastro_demo import __version__

            package_version = __version__
        self.package_version = package_version

    @property
    def access_log(self) -> Path:
        return self.state_dir / ACCESS_LOG

    def claim(self) -> None:
        """Posse exclusiva da pasta; outro token só assume depois que o dono liberou."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        owner = self.state_dir / OWNER_FILE
        try:
            fd = os.open(owner, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            current: dict[str, Any] = {}
            with contextlib.suppress(OSError, ValueError):
                current = json.loads(owner.read_text(encoding="utf-8"))
            if current.get("token") != self.owner_token:
                raise EvalError(
                    "lease_busy", "the evaluation demo is owned by another runner"
                ) from None
            return
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"token": self.owner_token, "pid": os.getpid()}, handle)

    def release(self) -> None:
        owner = self.state_dir / OWNER_FILE
        with contextlib.suppress(OSError, ValueError):
            if json.loads(owner.read_text(encoding="utf-8")).get("token") == self.owner_token:
                owner.unlink()

    def ensure(self, seed: int) -> str:
        """Sobe (ou reinicia) o demo com o seed do bloco; o seed só vale na inicialização."""
        from lastro_demo import runner

        self.claim()
        if self.seed == seed and self.url:
            return self.url
        runner.stop(state_dir=self.state_dir)
        state = runner.start(
            state_dir=self.state_dir,
            port=free_port(self.host),
            host=self.host,
            extra_env={SEED_ENV: str(seed)},
        )
        self.url, self.seed = state.url, seed
        return self.url

    def reset(self, demo: DemoSpec) -> ResetResult:
        try:
            with self._client() as client:
                response = client.post(f"{self.url}/__demo/reset", json=expected_flags(demo))
        except httpx.HTTPError as exc:
            return ResetResult(False, {}, f"{type(exc).__name__}")
        payload: dict[str, Any] = {}
        with contextlib.suppress(ValueError):
            payload = response.json()
        if response.status_code != 200 or payload.get("status") != "ok":
            return ResetResult(False, payload, f"http {response.status_code}")
        return ResetResult(True, payload)

    def version(self) -> dict[str, Any]:
        try:
            with self._client() as client:
                response = client.get(f"{self.url}/__demo/version")
            payload: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EvalError("version_mismatch", f"version unavailable: {exc}") from None
        return payload

    def log_offset(self) -> int:
        try:
            return self.access_log.stat().st_size
        except OSError:
            return 0

    def window(self, start: int, end: int) -> LogWindow:
        return read_window(self.access_log, start, end)

    def stop(self) -> None:
        from lastro_demo import runner

        with contextlib.suppress(Exception):
            runner.stop(state_dir=self.state_dir)
        self.url, self.seed = "", None
        self.release()
