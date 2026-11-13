"""Observabilidade opt-in: projeção do histórico em spans OTLP enviados ao Langfuse.

O SDK OpenTelemetry só é importado quando a exportação é habilitada e o extra `obs` existe;
a instalação base e as runs desligadas nunca carregam o SDK nem fazem rede.
"""

import importlib.util
import os
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lastro.config.loader import ProjectContext
    from lastro.config.observability import ObservabilitySettings
    from lastro.history import HistoryStore
    from lastro.observability.worker import ExportWorker

EXTRA_HINT = "uv sync --extra obs (ou pip install 'lastro[obs]')"
_SDK_MODULES = ("opentelemetry.sdk.trace", "opentelemetry.exporter.otlp.proto.common")


def extra_available() -> bool:
    try:
        return all(importlib.util.find_spec(name) is not None for name in _SDK_MODULES)
    except ModuleNotFoundError:
        return False


ERROR_MESSAGES = {
    "obs_extra_missing": "Observability is enabled but the 'obs' extra is not installed. "
    f"Install it with: {EXTRA_HINT}.",
}


class ObservabilityError(Exception):
    """Erro local de opt-in: o comando para antes de criar run ou tocar o alvo."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def preflight(settings: "ObservabilitySettings", environ: Mapping[str, str] | None = None) -> None:
    """Extra e chaves exigidos só quando habilitado; rede nunca participa do preflight."""
    if not settings.enabled:
        return
    if not extra_available():
        raise ObservabilityError("obs_extra_missing", ERROR_MESSAGES["obs_extra_missing"])
    source = environ if environ is not None else os.environ
    for name in (settings.public_key_env, settings.secret_key_env):
        if not source.get(name, "").strip():
            raise ObservabilityError(
                "obs_credentials_missing",
                f"Observability is enabled but the environment variable {name} is not set.",
            )


def settings_source(project: "ProjectContext") -> Callable[[], "ObservabilitySettings"]:
    """Releitura do perfil atual sem efeitos externos; inválido ou ausente suspende o envio."""
    from lastro.config.loader import load_config

    path, name = project.config_path, project.profile_name

    def read() -> "ObservabilitySettings":
        return load_config(path).profiles[name].observability

    return read


def start_worker(
    project: "ProjectContext", history: "HistoryStore", run_id: str
) -> "ExportWorker | None":
    """Worker em segundo plano só para run com opt-in congelado; nunca levanta exceção."""
    try:
        if history.observability_context(run_id) is None:
            return None
        from lastro.observability.worker import ExportWorker

        worker = ExportWorker(history, run_id, settings_source(project))
        worker.start()
        return worker
    except Exception as error:
        _warn(f"observability export unavailable ({type(error).__name__})")
        return None


def drain_run(
    project: "ProjectContext", history: "HistoryStore", run_id: str, budget: float = 5.0
) -> None:
    """Drenagem local limitada após término ou eventos posteriores; falha só gera aviso."""
    try:
        if history.observability_context(run_id) is None:
            return
        from lastro.observability.worker import ExportWorker

        result = ExportWorker(history, run_id, settings_source(project)).drain(budget)
        if result.last_code is not None:
            _warn(
                f"observability export incomplete ({result.last_code}); "
                f"run 'lastro obs export {run_id}' to retry"
            )
    except Exception as error:
        _warn(f"observability export unavailable ({type(error).__name__})")


def _warn(message: str) -> None:
    import sys

    print(f"lastro: warning: {message}", file=sys.stderr)


def without_keys(
    settings: "ObservabilitySettings", environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Ambiente de executor externo sem as chaves Langfuse; o MCP as relê do .env do projeto."""
    source = environ if environ is not None else os.environ
    hidden = {settings.public_key_env.upper(), settings.secret_key_env.upper()}
    return {key: value for key, value in source.items() if key.upper() not in hidden}
