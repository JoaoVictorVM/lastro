"""Grupo lastro autonomous: run manual headless, trigger pós-deploy, fila e retry de entregas.

Todo trigger vira um item durável antes de qualquer execução. Um trigger aceito retorna sucesso
mesmo quando outro worker possui o lease do perfil: o item já está na fila. Saída humana em pt-BR;
`--json` usa envelope estável. Uso inválido sai com 2, falha operacional com 1, interrupção 130.
"""

import json
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from lastro.autonomous.models import (
    INTERRUPTED_EXIT_CODE,
    INVALID_INPUT,
    AutonomousError,
    EnqueueResult,
    MissionSnapshot,
    QueueStatus,
    TriggerInput,
)
from lastro.autonomous.runner import ItemOutcome
from lastro.autonomous.scheduler import freeze_mission_file
from lastro.autonomous.triggers import (
    github_trigger,
    ignore_reason,
    manual_trigger,
    parse_event,
    read_event_file,
)
from lastro.cli import output
from lastro.cli.schedule_cmd import emit, fail
from lastro.cli.state import get_state
from lastro.config.loader import LoadedConfig, ProjectContext
from lastro.history import HistoryStore

type Drain = Callable[[LoadedConfig, HistoryStore, str], list[ItemOutcome]]


def _default_drain(loaded: LoadedConfig, store: HistoryStore, profile: str) -> list[ItemOutcome]:
    from lastro.autonomous.runner import drain_profile

    return drain_profile(loaded, store, profile, write=output.info)


# Ponto de injeção: os testes substituem o worker por um executor simulado.
drain: Drain = _default_drain

autonomous_app = typer.Typer(
    help="Execuções autônomas: run manual, triggers pós-deploy, fila e notificações.",
    no_args_is_help=True,
)

JsonFlag = Annotated[bool, typer.Option("--json", help="Resultado JSON em stdout.")]


def _context(ctx: typer.Context) -> tuple[ProjectContext, HistoryStore]:
    state = get_state(ctx)
    project = state.require_history_project()
    return project, state.open_history(project)


def _mission(
    project: ProjectContext, mission_file: Path | None, mission: str | None
) -> MissionSnapshot:
    if (mission_file is None) == (mission is None):
        raise AutonomousError(INVALID_INPUT, "provide exactly one of --mission-file or --mission")
    if mission_file is not None:
        return freeze_mission_file(project.loaded.project_dir, str(mission_file))
    assert mission is not None
    return MissionSnapshot("text", None, mission)


def _accept(
    project: ProjectContext, store: HistoryStore, trigger: TriggerInput, as_json: bool
) -> None:
    """Enfileira, tenta drenar e reporta o item; lease ocupado não é falha do trigger."""
    from lastro.autonomous.queue import AutonomousQueue

    queued: EnqueueResult = AutonomousQueue(store.autonomous).enqueue(trigger)
    try:
        outcomes = drain(project.loaded, store, trigger.profile)
    except KeyboardInterrupt:
        raise typer.Exit(code=INTERRUPTED_EXIT_CODE) from None
    mine = next((item for item in outcomes if item.item.id == queued.item.id), None)
    current = store.autonomous.get_item(queued.item.id)
    data: dict[str, Any] = {
        "status": current.status.value,
        "item_id": current.id,
        "trigger": current.trigger_kind.value,
        "profile": current.profile,
        "queue_position": queued.position,
        "worker_started": bool(outcomes),
        "duplicate": not queued.created,
        "dropped": [item.id for item in queued.dropped],
    }
    if mine is not None:
        data.update(
            {
                "run_id": mine.run.id if mine.run else None,
                "run_status": mine.run.status.value if mine.run else None,
                "reason": mine.item.reason,
                "report": str(mine.report_path) if mine.report_path else None,
            }
        )
    if as_json:
        emit(data)
        return
    if current.status == QueueStatus.QUEUED:
        output.info(
            f"Item {current.id} na fila do perfil {current.profile} "
            f"(posição {queued.position}); outro worker está ativo."
        )
    else:
        output.info(f"Item {current.id}: {current.status.value}")
    for dropped in queued.dropped:
        output.info(f"Descartado por capacidade da fila: {dropped.id}")
    if mine is not None and mine.run is not None:
        reason = f" ({mine.run.status_reason})" if mine.run.status_reason else ""
        output.info(f"Run {mine.run.id}: {mine.run.status.value}{reason}")
        if mine.report_path is not None:
            output.info(f"Relatório: {mine.report_path}")


@autonomous_app.command("run")
def run(
    ctx: typer.Context,
    mission_file: Annotated[
        Path | None, typer.Option("--mission-file", help="Arquivo da missão sob o projeto.")
    ] = None,
    mission: Annotated[
        str | None, typer.Option("--mission", help="Missão em texto, só no trigger manual.")
    ] = None,
    key: Annotated[
        str | None,
        typer.Option("--key", help="Chave idempotente; repetir com a mesma chave não duplica."),
    ] = None,
    as_json: JsonFlag = False,
) -> None:
    """Congela a missão, cria um trigger manual, enfileira e tenta drenar sem interação."""
    project, store = _context(ctx)
    try:
        snapshot = _mission(project, mission_file, mission)
        trigger = manual_trigger(
            project.profile_name,
            snapshot,
            executor=project.config.executor,
            key=key or f"manual:{secrets.token_hex(12)}",
        )
        _accept(project, store, trigger, as_json)
    except AutonomousError as exc:
        fail(exc, as_json)


@autonomous_app.command("trigger")
def trigger(
    ctx: typer.Context,
    event_file: Annotated[Path, typer.Option("--event-file", help="GITHUB_EVENT_PATH.")],
    event_name: Annotated[
        str, typer.Option("--event-name", help="workflow_run ou deployment_status.")
    ],
    mission_file: Annotated[
        Path, typer.Option("--mission-file", help="Missão definida pelo passo confiável.")
    ],
    fixture: Annotated[bool, typer.Option("--fixture", hidden=True)] = False,
    as_json: JsonFlag = False,
) -> None:
    """Só dentro do GitHub Actions: aceita campos em allowlist do evento de implantação."""
    project, store = _context(ctx)
    try:
        payload = read_event_file(event_file, event_name, fixture=fixture)
        event = parse_event(event_name, payload)
        reason = ignore_reason(event, project.config.autonomous.github)
        if reason is not None:
            data = {"status": "ignored", "reason": reason, "delivery_key": event.delivery_key}
            if as_json:
                emit(data)
            else:
                output.info(f"Evento ignorado: {reason}")
            return
        snapshot = freeze_mission_file(project.loaded.project_dir, str(mission_file))
        accepted = github_trigger(
            event, project.profile_name, snapshot, executor=project.config.executor
        )
        _accept(project, store, accepted, as_json)
    except AutonomousError as exc:
        fail(exc, as_json)


@autonomous_app.command("queue")
def queue(
    ctx: typer.Context,
    profile: Annotated[str | None, typer.Option("--profile", help="Filtra por perfil.")] = None,
    as_json: JsonFlag = False,
) -> None:
    """Item ativo e pendentes de cada perfil, sem texto da missão nem segredo."""
    state = get_state(ctx)
    loaded = state.require_history_config()
    store = state.open_history(loaded)
    try:
        items = store.autonomous.list_items(
            profile, (QueueStatus.CLAIMED, QueueStatus.RUNNING, QueueStatus.QUEUED)
        )
    except AutonomousError as exc:
        fail(exc, as_json)
    views = [item.public_json() for item in items]
    if as_json:
        emit({"status": "ok", "items": views})
        return
    if not views:
        output.info("Fila vazia.")
    for view in views:
        output.info(f"{view['item_id']} · {view['profile']} · {view['trigger']} · {view['status']}")


@autonomous_app.command("retry-notifications")
def retry_notifications(
    ctx: typer.Context,
    item: Annotated[str | None, typer.Option("--item", help="Só as entregas deste item.")] = None,
    as_json: JsonFlag = False,
) -> None:
    """Tenta as entregas vencidas; nunca repete a missão nem altera a run."""
    from lastro.autonomous.notifications import Outbox

    state = get_state(ctx)
    loaded = state.require_history_config()
    store = state.open_history(loaded)
    try:
        records = Outbox(store, loaded.config.autonomous).deliver_due(item)
    except AutonomousError as exc:
        fail(exc, as_json)
    views = [
        {
            "notification_id": record.id,
            "item_id": record.queue_item_id,
            "channel": record.channel_name,
            "status": record.status.value,
            "attempts": record.attempts,
            "error_code": record.last_error_code,
        }
        for record in records
    ]
    if as_json:
        typer.echo(json.dumps({"status": "ok", "deliveries": views}, sort_keys=True))
        return
    if not views:
        output.info("Nenhuma entrega vencida.")
    for view in views:
        output.info(f"{view['channel']}: {view['status']} (tentativas {view['attempts']})")
