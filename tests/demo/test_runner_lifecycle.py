import contextlib
import inspect
import json
import socket
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from lastro_demo import DEFAULT_HOST, DEFAULT_PORT, _process, runner


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def port_accepts(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def recorded_pids(state_dir: Path) -> set[int]:
    pids: set[int] = set()
    try:
        record = json.loads((state_dir / "server.json").read_text(encoding="utf-8"))
        pids.update(value for key, value in record.items() if key.endswith("pid"))
    except (OSError, ValueError):
        pass
    with contextlib.suppress(OSError, ValueError):
        pids.add(int((state_dir / "server.pid").read_text(encoding="utf-8")))
    return pids


@pytest.fixture
def lifecycle(state_dir: Path) -> Iterator[Callable[..., runner.DemoState]]:
    """Inicia servidores reais em porta efêmera e garante que nenhum processo sobrevive ao teste."""
    seen: set[int] = set()

    def start(**kwargs: Any) -> runner.DemoState:
        kwargs.setdefault("port", free_port())
        state = runner.start(state_dir=state_dir, **kwargs)
        seen.update(recorded_pids(state_dir))
        return state

    yield start
    seen.update(recorded_pids(state_dir))
    try:
        runner.stop(state_dir=state_dir)
    finally:
        for pid in seen:
            if _process.pid_alive(pid):
                _process.force_kill(pid)
                _process.wait_exit(pid, 5)
    assert not any(_process.pid_alive(pid) for pid in seen)


def test_default_port_and_host_constants() -> None:
    parameters = inspect.signature(runner.start).parameters

    assert DEFAULT_PORT == 8765
    assert DEFAULT_HOST == "127.0.0.1"
    assert parameters["port"].default == DEFAULT_PORT
    assert parameters["host"].default == DEFAULT_HOST


def test_start_serves_and_reports_ready(
    lifecycle: Callable[..., runner.DemoState], state_dir: Path
) -> None:
    state = lifecycle()

    assert state.ready_message == "Demo ready: 2 clinics, 40 employees, 10 defects active"
    assert (state.clinics, state.employees) == (2, 40)
    assert state.url == f"http://127.0.0.1:{state.port}"
    assert state.credential_env_vars == (
        "DEMO_AURORA_USER",
        "DEMO_AURORA_PASSWORD",
        "DEMO_BOREAL_USER",
        "DEMO_BOREAL_PASSWORD",
    )
    assert state.scenarios == ("s01", "s02")
    assert state.access_log_path == state_dir / "access.log"
    assert (state_dir / "server.json").is_file()
    with httpx.Client(base_url=state.url, trust_env=False) as client:
        assert client.get("/__demo/version").json()["defects"] == list(state.defects)
        started = time.perf_counter()
        reset = client.post("/__demo/reset")
        elapsed = time.perf_counter() - started
        assert reset.status_code == 200
        assert reset.json()["employees"] == 40
        assert elapsed < 2.0
        assert client.get("/login").status_code == 200
    assert state.access_log_path.read_text(encoding="utf-8").count("/__demo/reset") == 1
    status = runner.status(state_dir=state_dir)
    assert status.state == "running"
    assert status.url == state.url


def test_start_is_idempotent(lifecycle: Callable[..., runner.DemoState], state_dir: Path) -> None:
    first = lifecycle()

    second = runner.start(state_dir=state_dir, port=free_port())

    assert second.pid == first.pid
    assert second.url == first.url
    assert second.ready_message == first.ready_message


def test_stop_terminates_and_cleans_state(
    lifecycle: Callable[..., runner.DemoState], state_dir: Path
) -> None:
    state = lifecycle()
    pids = recorded_pids(state_dir)

    result = runner.stop(state_dir=state_dir)

    assert result.was_running
    assert not (state_dir / "server.json").exists()
    assert not port_accepts(state.port)
    assert not any(_process.pid_alive(pid) for pid in pids)
    assert runner.status(state_dir=state_dir).state == "stopped"
    again = runner.stop(state_dir=state_dir)
    assert not again.was_running


def test_stale_state_is_recovered(state_dir: Path) -> None:
    state_dir.mkdir(parents=True)
    port = free_port()
    record = {
        "pid": 2_000_000_000,
        "host": "127.0.0.1",
        "port": port,
        "url": f"http://127.0.0.1:{port}",
    }
    (state_dir / "server.json").write_text(json.dumps(record), encoding="utf-8")

    assert runner.status(state_dir=state_dir).state == "stale"
    assert not (state_dir / "server.json").exists()
    (state_dir / "server.json").write_text(json.dumps(record), encoding="utf-8")
    result = runner.stop(state_dir=state_dir)
    assert not result.was_running
    assert result.message is not None
    assert not (state_dir / "server.json").exists()
    (state_dir / "server.json").write_text("{corrompido", encoding="utf-8")
    assert runner.status(state_dir=state_dir).state == "stopped"


def test_port_in_use_raises_clear_error(state_dir: Path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = int(busy.getsockname()[1])

        with pytest.raises(runner.PortInUseError) as info:
            runner.start(state_dir=state_dir, port=port)

    assert str(port) in str(info.value)
    assert "--port" in str(info.value)
    assert not (state_dir / "server.json").exists()


def test_ready_message_reflects_active_defects(
    lifecycle: Callable[..., runner.DemoState],
) -> None:
    state = lifecycle(defects=["d01", "d05", "d09"], scenarios=[])

    assert state.ready_message == "Demo ready: 2 clinics, 40 employees, 3 defects active"
    assert state.defects == ("d01", "d05", "d09")
    assert state.scenarios == ()


def test_not_ready_reports_server_log(state_dir: Path) -> None:
    with pytest.raises(runner.DemoNotReadyError) as info:
        runner.start(state_dir=state_dir, port=free_port(), ready_timeout=0.01)

    assert str(state_dir / "server.log") in str(info.value)
    assert not (state_dir / "server.json").exists()
    time.sleep(0.5)
    assert not any(_process.pid_alive(pid) for pid in recorded_pids(state_dir))


def test_unwritable_state_dir_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "arquivo"
    blocker.write_text("x", encoding="utf-8")

    with pytest.raises(runner.StateDirNotWritableError) as info:
        runner.start(state_dir=blocker / "demo", port=free_port())

    assert str(blocker / "demo") in str(info.value)


def test_unknown_flag_rejected_before_spawn(state_dir: Path) -> None:
    with pytest.raises(ValueError, match="d42"):
        runner.start(state_dir=state_dir, port=free_port(), defects=["d42"])

    assert not (state_dir / "server.json").exists()
