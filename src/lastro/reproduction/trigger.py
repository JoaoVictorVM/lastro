"""Gatilho automático da reprodução ao fim de uma run completed.

Native e codex reproduzem no processo da CLI que encerrou a run. No claude-code, o owner que fecha
a run inicia um processo desacoplado `lastro run reproduce --auto`, reconciliável pela rodada.
Falhar aqui nunca invalida a run explorada: os findings ficam suspected e o relatório indica o
comando para reproduzir depois.
"""

import contextlib
import os
import subprocess
import sys
from collections.abc import Callable

from lastro.config.loader import ProjectContext
from lastro.config.reproduction import settings_from_snapshot
from lastro.history import (
    REPRODUCTION_CHILD_BLOCK,
    HistoryError,
    HistoryStore,
    RunRecord,
    RunStatus,
)
from lastro.reproduction.coordinator import ReproductionCoordinator, RoundOutcome

type Writer = Callable[[str], None]
type Spawn = Callable[[ProjectContext, str], None]


def pending_message(run_id: str) -> str:
    return f"Reproduction pending: run 'lastro run reproduce {run_id}'."


def should_reproduce(project: ProjectContext, record: RunRecord) -> bool:
    """Só completed dispara sozinho; run filha, auto desligado ou outros terminais, não."""
    if record.status != RunStatus.COMPLETED:
        return False
    if REPRODUCTION_CHILD_BLOCK in record.config_snapshot:
        return False
    settings = settings_from_snapshot(
        record.config_snapshot.get("reproduction"), project.profile.reproduction
    )
    return settings.auto


def auto_reproduce(
    project: ProjectContext,
    history: HistoryStore,
    run_id: str,
    *,
    write: Writer,
    coordinator: ReproductionCoordinator | None = None,
) -> RoundOutcome | None:
    try:
        record = history.get_run(run_id)
        if not should_reproduce(project, record) or not history.eligible_findings(run_id):
            return None
        runner = coordinator or ReproductionCoordinator(project, history, write=write)
        return runner.run(run_id)
    except HistoryError as error:
        write(f"! reproduction did not start: {error}")
        write(pending_message(run_id))
        return None


def spawn_detached(project: ProjectContext, run_id: str) -> None:
    """Processo filho fora do grupo do owner: sobrevive ao fechamento da sessão do executor."""
    arguments = [
        sys.executable,
        "-m",
        "lastro",
        "--config",
        str(project.config_path),
        "--profile",
        project.profile_name,
        "run",
        "reproduce",
        run_id,
        "--auto",
    ]
    options: dict[str, object] = {
        "cwd": str(project.loaded.project_dir),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        options["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # pyright: ignore[reportAttributeAccessIssue]
        )
    else:
        options["start_new_session"] = True
    subprocess.Popen(arguments, **options)  # pyright: ignore[reportArgumentType, reportCallIssue]


# Ponto de injeção dos testes: o owner do claude-code não inicia processos reais nos testes.
SPAWN: Spawn = spawn_detached


def detach_after_owner(project: ProjectContext, record: RunRecord) -> bool:
    """Chamado pelo owner do claude-code ao encerrar; nunca propaga falha para a run."""
    if record.executor_name != "claude-code" or not should_reproduce(project, record):
        return False
    with contextlib.suppress(Exception):
        SPAWN(project, record.id)
        return True
    return False
