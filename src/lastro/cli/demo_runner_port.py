"""Ponte para o provedor do demo (`lastro_demo.runner`), carregado só em tempo de execução.

Contrato esperado do provedor (funções do módulo, chamadas sempre com argumentos nomeados):

- ``start(*, state_dir: Path, port: int, host: str)`` devolve um objeto com ``ready_message: str``,
  ``url: str`` e ``credential_env_vars: Sequence[str]``; conjunto de defeitos e variante não são
  informados, valendo o padrão do provedor.
- ``stop(*, state_dir: Path)`` devolve um objeto com ``was_running: bool`` e, opcionalmente,
  ``message: str | None``.
- ``status(*, state_dir: Path)`` devolve o estado atual (rodando, parado ou obsoleto).

Falhas do provedor são exceções; a mensagem da exceção é mostrada ao usuário.
"""

import importlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, cast

PROVIDER_PACKAGE = "lastro_demo"
RUNNER_MODULE = f"{PROVIDER_PACKAGE}.runner"
REQUIRED_OPERATIONS = ("start", "stop", "status")
DEMO_HOST = "127.0.0.1"
START_FIELDS = ("ready_message", "url", "credential_env_vars")


class DemoProviderError(Exception):
    pass


class DemoUnavailableError(DemoProviderError):
    pass


class DemoIncompatibleError(DemoProviderError):
    pass


class DemoOperationError(DemoProviderError):
    pass


@dataclass(frozen=True)
class StartOutcome:
    ready_message: str
    url: str
    credential_env_vars: tuple[str, ...]


@dataclass(frozen=True)
class StopOutcome:
    was_running: bool
    message: str | None


def _unavailable(detail: str) -> DemoUnavailableError:
    return DemoUnavailableError(
        f"O demo não está disponível nesta instalação ({detail}). Ele é fornecido pelo pacote "
        f"{PROVIDER_PACKAGE}, distribuído junto com o lastro: reinstale a partir do repositório "
        "com uv tool install --reinstall <caminho ou URL do repositório>."
    )


def _incompatible(detail: str) -> DemoIncompatibleError:
    return DemoIncompatibleError(
        f"A versão do demo instalada é incompatível com esta versão do lastro ({detail}). "
        "Reinstale o lastro a partir do repositório para alinhar as duas partes."
    )


def _failure_message(exc: Exception) -> str:
    return str(exc) or type(exc).__name__


class DemoRunnerPort:
    def __init__(self, module: ModuleType) -> None:
        self._module = module

    def _operation(self, name: str) -> Callable[..., Any]:
        return cast(Callable[..., Any], getattr(self._module, name))

    def start(self, state_dir: Path, port: int) -> StartOutcome:
        try:
            state = self._operation("start")(state_dir=state_dir, port=port, host=DEMO_HOST)
        except Exception as exc:
            raise DemoOperationError(_failure_message(exc)) from exc
        missing = [field for field in START_FIELDS if not hasattr(state, field)]
        if missing:
            raise _incompatible(f"resultado de start sem {', '.join(missing)}")
        variables = cast(Sequence[str], state.credential_env_vars)
        return StartOutcome(
            ready_message=str(state.ready_message),
            url=str(state.url),
            credential_env_vars=tuple(str(name) for name in variables),
        )

    def stop(self, state_dir: Path) -> StopOutcome:
        try:
            result = self._operation("stop")(state_dir=state_dir)
        except Exception as exc:
            raise DemoOperationError(_failure_message(exc)) from exc
        if not hasattr(result, "was_running"):
            raise _incompatible("resultado de stop sem was_running")
        message = getattr(result, "message", None)
        return StopOutcome(
            was_running=bool(result.was_running),
            message=None if message is None else str(message),
        )


def load_demo_runner(
    import_module: Callable[[str], ModuleType] = importlib.import_module,
) -> DemoRunnerPort:
    try:
        module = import_module(RUNNER_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name in (PROVIDER_PACKAGE, RUNNER_MODULE):
            raise _unavailable(f"módulo {RUNNER_MODULE} não encontrado") from None
        raise _unavailable(f"dependência ausente: {exc.name}") from None
    except ImportError as exc:
        raise _unavailable(f"falha ao carregar {RUNNER_MODULE}: {exc}") from None
    missing = [name for name in REQUIRED_OPERATIONS if not callable(getattr(module, name, None))]
    if missing:
        raise _incompatible(f"operações ausentes em {RUNNER_MODULE}: {', '.join(missing)}")
    return DemoRunnerPort(module)


def provider_default_port() -> int | None:
    """Constante de porta padrão do provedor, ou None quando ele não está instalado."""
    try:
        package = importlib.import_module(PROVIDER_PACKAGE)
    except ImportError:
        return None
    value = getattr(package, "DEFAULT_PORT", None)
    return value if isinstance(value, int) else None
