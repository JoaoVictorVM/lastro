import json
import socket
from collections.abc import Callable
from pathlib import Path

import httpx
from typer.testing import CliRunner

from lastro.cli.app import app
from lastro.paths import default_private_data_root
from lastro_demo import _process


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def demo_config(private_root: Path, port: int, profiles: str) -> str:
    """lastro.toml com private data root temporário, para nunca tocar no do usuário."""
    return f"private_data_root = '{private_root.as_posix()}'\n\n[demo]\nport = {port}\n{profiles}"


def server_pids(state_dir: Path) -> set[int]:
    pids: set[int] = set()
    try:
        record = json.loads((state_dir / "server.json").read_text(encoding="utf-8"))
        pids.update(value for key, value in record.items() if key.endswith("pid"))
    except (OSError, ValueError):
        pass
    return pids


def test_cli_demo_up_reset_down_with_real_runner(
    runner: CliRunner,
    write_config: Callable[..., Path],
    profile_toml: Callable[..., str],
    workdir: Path,
) -> None:
    private_root = workdir.parent / "private-root"
    port = free_port()
    write_config(demo_config(private_root, port, profile_toml()))
    state_dir = private_root / "demo"
    pids: set[int] = set()
    try:
        up = runner.invoke(app, ["demo", "up"])
        pids = server_pids(state_dir)

        assert up.exit_code == 0, up.output
        assert "Demo ready: 2 clinics, 40 employees, 10 defects active" in up.stdout
        assert f"URL: http://127.0.0.1:{port}" in up.stdout
        for name in ("DEMO_AURORA_USER", "DEMO_AURORA_PASSWORD", "DEMO_BOREAL_USER"):
            assert name in up.stdout
        assert pids
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            reset = client.post("/__demo/reset", json={"defects": ["d01"]})
            assert reset.status_code == 200
            assert reset.json()["employees"] == 40
            assert client.get("/__demo/version").json()["defects"] == ["d01"]
        again = runner.invoke(app, ["demo", "up"])
        assert again.exit_code == 0, again.output
        assert "Demo ready: 2 clinics, 40 employees, 1 defects active" in again.stdout
        assert server_pids(state_dir) == pids

        down = runner.invoke(app, ["demo", "down"])

        assert down.exit_code == 0, down.output
        assert "Demo parado." in down.stdout
        assert not (state_dir / "server.json").exists()
        assert not any(_process.pid_alive(pid) for pid in pids)
        after = runner.invoke(app, ["demo", "down"])
        assert after.exit_code == 0
        assert "não estava rodando" in after.stdout
        assert not (default_private_data_root() / "demo").exists()
    finally:
        for pid in pids:
            if _process.pid_alive(pid):
                _process.force_kill(pid)


def test_cli_demo_up_port_in_use_exits_1(
    runner: CliRunner,
    write_config: Callable[..., Path],
    profile_toml: Callable[..., str],
    workdir: Path,
) -> None:
    private_root = workdir.parent / "private-root"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = int(busy.getsockname()[1])
        write_config(demo_config(private_root, port, profile_toml()))

        result = runner.invoke(app, ["demo", "up"])

    assert result.exit_code == 1
    assert f"A porta {port} já está em uso" in result.stderr
