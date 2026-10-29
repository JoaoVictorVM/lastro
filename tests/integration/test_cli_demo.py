import importlib
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from lastro.cli.app import app
from lastro.cli.demo_runner_port import RUNNER_MODULE, provider_default_port
from lastro.config.schema import DEFAULT_DEMO_PORT, DemoSettings
from lastro.paths import default_private_data_root

READY = "Demo ready: 2 clinics, 40 employees, 10 defects active"
CREDENTIAL_VARS = [
    "DEMO_AURORA_USERNAME",
    "DEMO_AURORA_PASSWORD",
    "DEMO_BOREAL_USERNAME",
    "DEMO_BOREAL_PASSWORD",
]


@dataclass
class FakeRunner:
    """Duplo de teste do provedor do demo, registrado no lugar do módulo real."""

    calls: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list[tuple[str, dict[str, Any]]]
    )
    was_running: bool = True
    fail_with: Exception | None = None

    def module(self) -> ModuleType:
        module = ModuleType(RUNNER_MODULE)

        def start(**kwargs: Any) -> SimpleNamespace:
            self.calls.append(("start", kwargs))
            if self.fail_with is not None:
                raise self.fail_with
            url = f"http://{kwargs['host']}:{kwargs['port']}"
            return SimpleNamespace(
                ready_message=READY,
                url=url,
                pid=1234,
                flags=[],
                credential_env_vars=CREDENTIAL_VARS,
            )

        def stop(**kwargs: Any) -> SimpleNamespace:
            self.calls.append(("stop", kwargs))
            if self.fail_with is not None:
                raise self.fail_with
            return SimpleNamespace(was_running=self.was_running)

        def status(**kwargs: Any) -> SimpleNamespace:
            self.calls.append(("status", kwargs))
            return SimpleNamespace(state="stopped", url=None, flags=[])

        module.start = start  # type: ignore[attr-defined]
        module.stop = stop  # type: ignore[attr-defined]
        module.status = status  # type: ignore[attr-defined]
        return module


@pytest.fixture
def fake_runner(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    runner = FakeRunner()
    monkeypatch.setitem(sys.modules, RUNNER_MODULE, runner.module())
    return runner


def test_demo_group_registered(runner: CliRunner) -> None:
    result = runner.invoke(app, ["demo", "--help"])

    assert result.exit_code == 0
    assert "up" in result.stdout
    assert "down" in result.stdout


def test_demo_up_without_provider_degrades(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, RUNNER_MODULE, None)

    for command in ("up", "down"):
        result = runner.invoke(app, ["demo", command])

        assert result.exit_code == 1
        assert "não está disponível nesta instalação" in result.stderr
        assert "lastro_demo" in result.stderr
    assert not (default_private_data_root() / "demo").exists()
    assert runner.invoke(app, ["--help"]).exit_code == 0
    assert runner.invoke(app, ["demo", "--help"]).exit_code == 0


def test_demo_up_incompatible_provider(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    module = ModuleType(RUNNER_MODULE)
    module.start = lambda **kwargs: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, RUNNER_MODULE, module)

    result = runner.invoke(app, ["demo", "up"])

    assert result.exit_code == 1
    assert "incompatível" in result.stderr
    assert "stop, status" in result.stderr


def test_demo_up_incompatible_result(
    runner: CliRunner, fake_runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = sys.modules[RUNNER_MODULE]
    monkeypatch.setattr(module, "start", lambda **kwargs: SimpleNamespace(url="x"))

    result = runner.invoke(app, ["demo", "up"])

    assert result.exit_code == 1
    assert "incompatível" in result.stderr


def _started_port(fake: FakeRunner) -> int:
    name, kwargs = fake.calls[-1]
    assert name == "start"
    return kwargs["port"]


def test_demo_port_resolution_order(
    runner: CliRunner,
    fake_runner: FakeRunner,
    write_config: Callable[..., Path],
    profile_toml: Callable[..., str],
) -> None:
    assert runner.invoke(app, ["demo", "up"]).exit_code == 0
    assert _started_port(fake_runner) == 8765

    write_config("[demo]\nport = 9100\n" + profile_toml())
    assert runner.invoke(app, ["demo", "up"]).exit_code == 0
    assert _started_port(fake_runner) == 9100

    assert runner.invoke(app, ["demo", "up", "--port", "9200"]).exit_code == 0
    assert _started_port(fake_runner) == 9200


def test_demo_port_out_of_range_rejected(runner: CliRunner, fake_runner: FakeRunner) -> None:
    result = runner.invoke(app, ["demo", "up", "--port", "80"])

    assert result.exit_code == 2
    assert fake_runner.calls == []


def test_demo_delegates_with_resolved_port(runner: CliRunner, fake_runner: FakeRunner) -> None:
    result = runner.invoke(app, ["demo", "up", "--port", "9300"])

    assert result.exit_code == 0, result.output
    name, kwargs = fake_runner.calls[-1]
    assert name == "start"
    assert kwargs == {
        "state_dir": default_private_data_root() / "demo",
        "port": 9300,
        "host": "127.0.0.1",
    }
    lines = result.stdout.splitlines()
    assert lines[0] == READY
    assert "URL: http://127.0.0.1:9300" in lines
    for variable in CREDENTIAL_VARS:
        assert f"  {variable}" in lines


def test_demo_state_dir_is_demo_subfolder_of_private_root(
    runner: CliRunner,
    fake_runner: FakeRunner,
    write_config: Callable[..., Path],
    profile_toml: Callable[..., str],
    workdir: Path,
) -> None:
    root = workdir.parent / "meu-root"
    write_config(f"private_data_root = '{root.as_posix()}'\n" + profile_toml())

    assert runner.invoke(app, ["demo", "up"]).exit_code == 0
    assert runner.invoke(app, ["demo", "down"]).exit_code == 0

    assert [kwargs["state_dir"] for _, kwargs in fake_runner.calls] == [root / "demo"] * 2
    assert (root / "demo").is_dir()


def test_demo_works_without_lastro_toml(
    runner: CliRunner, fake_runner: FakeRunner, workdir: Path
) -> None:
    assert not (workdir / "lastro.toml").exists()

    result = runner.invoke(app, ["demo", "up"])

    assert result.exit_code == 0, result.output
    assert _started_port(fake_runner) == 8765


def test_demo_with_invalid_config_does_not_delegate(
    runner: CliRunner, fake_runner: FakeRunner, write_config: Callable[..., Path]
) -> None:
    write_config("[demo]\nport = 80\n")

    result = runner.invoke(app, ["demo", "up"])

    assert result.exit_code == 2
    assert "lastro.toml › demo.port: expected integer between 1024 and 65535" in result.stderr
    assert fake_runner.calls == []


def test_demo_default_port_matches_lastro_demo_constant() -> None:
    try:
        importlib.import_module("lastro_demo")
    except ImportError:
        pytest.skip("pacote lastro_demo não instalado")

    assert provider_default_port() == DemoSettings().port == DEFAULT_DEMO_PORT


def test_demo_down_when_not_running_not_fatal(runner: CliRunner, fake_runner: FakeRunner) -> None:
    fake_runner.was_running = False

    result = runner.invoke(app, ["demo", "down"])

    assert result.exit_code == 0
    assert "não estava rodando" in result.stdout
    assert fake_runner.calls[-1] == ("stop", {"state_dir": default_private_data_root() / "demo"})


def test_demo_down_when_running(runner: CliRunner, fake_runner: FakeRunner) -> None:
    result = runner.invoke(app, ["demo", "down"])

    assert result.exit_code == 0
    assert "Demo parado." in result.stdout


def test_demo_provider_failure_exit_1(runner: CliRunner, fake_runner: FakeRunner) -> None:
    fake_runner.fail_with = OSError("porta 8765 ocupada por outro processo")

    up = runner.invoke(app, ["demo", "up"])
    down = runner.invoke(app, ["demo", "down"])

    assert up.exit_code == 1
    assert "porta 8765 ocupada por outro processo" in up.stderr
    assert down.exit_code == 1
    assert "Falha ao parar o demo" in down.stderr
