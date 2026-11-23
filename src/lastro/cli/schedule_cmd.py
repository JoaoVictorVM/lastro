"""Grupo lastro schedule: agenda diária reconciliada com o Task Scheduler ou o cron.

O comando instalado no SO só chama `lastro schedule tick <id>`; missão, webhooks e segredos nunca
entram nele. Saída humana em pt-BR; `--json` usa envelope estável. Uso inválido sai com 2,
falha operacional com 1.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from lastro.autonomous.models import (
    OPERATIONAL_EXIT_CODE,
    SCHEDULER_UNAVAILABLE,
    USAGE_CODES,
    USAGE_EXIT_CODE,
    AutonomousError,
    InstallState,
    ScheduleDefinition,
    ScheduleState,
    new_id,
)
from lastro.autonomous.scheduler import (
    SchedulerBackend,
    default_backend,
    next_due,
    parse_daily,
    safe_mission_path,
    schedule_fingerprint,
    tick_command,
    validate_name,
)
from lastro.cli import output
from lastro.cli.state import get_state
from lastro.config.errors import ConfigError
from lastro.config.loader import LoadedConfig, ProjectContext, select_profile
from lastro.history import HistoryError, HistoryStore
from lastro.history.database import db_timestamp
from lastro.timeutil import utc_now

type BackendFactory = Callable[[], SchedulerBackend]
type Drain = Callable[[LoadedConfig, HistoryStore, str], None]


def _default_drain(loaded: LoadedConfig, store: HistoryStore, profile: str) -> None:
    from lastro.autonomous.notifications import Outbox
    from lastro.autonomous.runner import drain_profile

    drain_profile(loaded, store, profile, write=output.info)
    # Um tick posterior drena entregas vencidas mesmo quando nenhuma agenda venceu.
    Outbox(store, loaded.config.autonomous).deliver_due()


# Pontos de injeção: os testes nunca tocam o Task Scheduler nem a crontab da máquina.
backend_factory: BackendFactory = default_backend
# Worker que drena a fila do perfil depois do tick; o coordenador autônomo o substitui.
drain: Drain = _default_drain

schedule_app = typer.Typer(
    help="Agenda diária de missões autônomas (Task Scheduler ou cron).",
    no_args_is_help=True,
)

JsonFlag = Annotated[bool, typer.Option("--json", help="Resultado JSON em stdout.")]


def fail(error: AutonomousError, as_json: bool) -> NoReturn:
    code = USAGE_EXIT_CODE if error.code in USAGE_CODES else OPERATIONAL_EXIT_CODE
    if as_json:
        typer.echo(
            json.dumps(
                {"status": "error", "code": error.code, "message": error.message},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        output.error(f"{error.message} ({error.code})")
    raise typer.Exit(code=code)


def emit(data: dict[str, Any]) -> None:
    typer.echo(json.dumps(data, ensure_ascii=False, sort_keys=True))


def _open(ctx: typer.Context) -> tuple[LoadedConfig, HistoryStore]:
    state = get_state(ctx)
    loaded = state.require_history_config()
    return loaded, state.open_history(loaded)


def _command(loaded: LoadedConfig, definition: ScheduleDefinition) -> list[str]:
    return tick_command(loaded.path, definition.id)


def _install_state(
    backend: SchedulerBackend, loaded: LoadedConfig, definition: ScheduleDefinition
) -> InstallState:
    if definition.state == ScheduleState.DISABLED:
        return InstallState.DISABLED
    try:
        return backend.inspect(definition, _command(loaded, definition))
    except AutonomousError:
        return InstallState.MISSING


def _view(
    definition: ScheduleDefinition, state: InstallState, backend: str, status: str | None = None
) -> dict[str, Any]:
    due = next_due(definition.daily_time, definition.timezone, utc_now())
    return {
        "status": status or state.value,
        "schedule_id": definition.id,
        "name": definition.name,
        "profile": definition.profile,
        "mission": definition.mission_path,
        "daily": definition.daily_time,
        "timezone": definition.timezone,
        "executor": definition.executor,
        "backend": backend,
        "external_id": definition.external_id,
        "next_due_at": db_timestamp(due),
        "drift": state == InstallState.DRIFTED,
        "install_state": state.value,
    }


def _print(view: dict[str, Any]) -> None:
    output.info(f"Agenda: {view['name']} ({view['schedule_id']})")
    output.info(
        f"Perfil {view['profile']} · diária às {view['daily']} ({view['timezone']}) · "
        f"missão {view['mission']}"
    )
    output.info(f"Backend: {view['backend']} · estado: {view['install_state']}")
    output.info(f"Próxima ocorrência (UTC): {view['next_due_at']}")


@schedule_app.command("add")
def add(
    ctx: typer.Context,
    name: Annotated[str, typer.Option("--name", help="Nome estável e único no projeto.")],
    daily: Annotated[str, typer.Option("--daily", help="Horário local HH:MM do projeto.")],
    mission: Annotated[Path, typer.Option("--mission", help="Arquivo da missão sob o projeto.")],
    profile: Annotated[
        str | None, typer.Option("--profile", help="Perfil existente no lastro.toml.")
    ] = None,
    replace: Annotated[
        bool, typer.Option("--replace", help="Substitui uma agenda existente diferente.")
    ] = False,
    as_json: JsonFlag = False,
) -> None:
    """Grava a agenda e instala a tarefa do SO que chama o tick a cada minuto."""
    state = get_state(ctx)
    loaded, store = _open(ctx)
    try:
        validate_name(name)
        parse_daily(daily)
        try:
            chosen = select_profile(loaded.config, profile or state.profile)
        except ConfigError as exc:
            raise AutonomousError("invalid_autonomous_input", str(exc)) from None
        relative = safe_mission_path(loaded.project_dir, str(mission))
        executor = loaded.config.executor
        timezone = loaded.config.timezone
        backend = backend_factory()
        fingerprint = schedule_fingerprint(name, chosen.name, relative, daily, timezone, executor)
        candidate = ScheduleDefinition(
            id=new_id("asch"),
            project_id=store.project_id,
            name=name,
            profile=chosen.name,
            mission_path=relative,
            daily_time=daily,
            timezone=timezone,
            executor=executor,
            settings={"autonomous": loaded.config.autonomous.snapshot()},
            fingerprint=fingerprint,
            backend=backend.name,
            external_id=None,
            state=ScheduleState.ACTIVE,
            created_at="",
            updated_at="",
            removed_at=None,
        )
        saved, changed = store.autonomous.save_schedule(candidate, replace=replace)
        if not changed:
            current = _install_state(backend, loaded, saved)
            view = _view(saved, current, backend.name)
        else:
            try:
                external = backend.install(saved, _command(loaded, saved))
            except AutonomousError as exc:
                # O manifesto fica gravado para uma reconciliação posterior repetir a instalação.
                raise AutonomousError(
                    SCHEDULER_UNAVAILABLE,
                    f"{exc.message}; the schedule was saved and can be reinstalled with "
                    "schedule add --replace",
                ) from None
            saved = store.autonomous.set_external(saved.id, backend.name, external)
            view = _view(saved, InstallState.INSTALLED, backend.name)
    except AutonomousError as exc:
        fail(exc, as_json)
    if as_json:
        emit(view)
    else:
        _print(view)


@schedule_app.command("list")
def list_schedules(
    ctx: typer.Context,
    profile: Annotated[str | None, typer.Option("--profile", help="Filtra por perfil.")] = None,
    as_json: JsonFlag = False,
) -> None:
    """Agendas do projeto com próxima ocorrência e reconciliação com o SO."""
    loaded, store = _open(ctx)
    try:
        backend = backend_factory()
        views = [
            _view(item, _install_state(backend, loaded, item), backend.name)
            for item in store.autonomous.list_schedules(profile)
        ]
    except AutonomousError as exc:
        fail(exc, as_json)
    if as_json:
        emit({"status": "ok", "schedules": views})
        return
    if not views:
        output.info("Nenhuma agenda cadastrada.")
    for view in views:
        _print(view)


def _find(store: HistoryStore, reference: str) -> ScheduleDefinition:
    found = store.autonomous.find_schedule(reference)
    if found is None or found.state == ScheduleState.REMOVED:
        raise AutonomousError("invalid_autonomous_input", f"schedule not found: {reference}")
    return found


@schedule_app.command("show")
def show(
    ctx: typer.Context,
    reference: Annotated[str, typer.Argument(help="Id ou nome da agenda.")],
    as_json: JsonFlag = False,
) -> None:
    """Manifesto redigido, última ocorrência e estado no SO (drift nunca é reinstalado)."""
    loaded, store = _open(ctx)
    try:
        definition = _find(store, reference)
        backend = backend_factory()
        view = _view(definition, _install_state(backend, loaded, definition), backend.name)
        last = store.autonomous.last_occurrence(definition.id)
        view["last_occurrence"] = (
            {
                "local_date": last.local_date,
                "status": last.status.value,
                "item_id": last.item_id,
                "reason": last.reason,
            }
            if last is not None
            else None
        )
        view["fingerprint"] = definition.fingerprint
    except AutonomousError as exc:
        fail(exc, as_json)
    if as_json:
        emit(view)
        return
    _print(view)
    if view["last_occurrence"] is not None:
        occurrence = view["last_occurrence"]
        output.info(f"Última ocorrência: {occurrence['local_date']} · {occurrence['status']}")


@schedule_app.command("remove")
def remove(
    ctx: typer.Context,
    reference: Annotated[str, typer.Argument(help="Id ou nome da agenda.")],
    as_json: JsonFlag = False,
) -> None:
    """Remove primeiro a tarefa do SO e depois marca a agenda como removida."""
    _, store = _open(ctx)
    try:
        definition = _find(store, reference)
        backend = backend_factory()
        backend.remove(definition)
        removed = store.autonomous.set_schedule_state(definition.id, ScheduleState.REMOVED)
    except AutonomousError as exc:
        fail(exc, as_json)
    view = {
        "status": "removed",
        "schedule_id": removed.id,
        "name": removed.name,
        "backend": backend.name,
    }
    if as_json:
        emit(view)
    else:
        output.info(f"Agenda {removed.name} removida do {backend.name} e do histórico.")


@schedule_app.command("tick", hidden=True)
def tick_cmd(
    ctx: typer.Context,
    schedule_id: Annotated[str, typer.Argument(help="Id opaco da agenda.")],
    as_json: JsonFlag = False,
) -> None:
    """Chamado pelo agendador do SO: decide vencimento, enfileira e drena a fila."""
    from lastro.autonomous.queue import AutonomousQueue
    from lastro.autonomous.scheduler import tick

    loaded, store = _open(ctx)
    try:
        definition = store.autonomous.find_schedule(schedule_id)
        if definition is None or definition.id != schedule_id:
            raise AutonomousError("invalid_autonomous_input", "schedule not found")
        queue = AutonomousQueue(store.autonomous)
        outcome = tick(store.autonomous, definition, loaded.project_dir, utc_now(), queue.enqueue)
    except AutonomousError as exc:
        fail(exc, as_json)
    except HistoryError as exc:
        fail(AutonomousError("queue_unavailable", str(exc)), as_json)
    if as_json:
        emit(outcome.as_json())
    elif outcome.local_date is not None:
        output.info(f"Agenda {definition.name}: {outcome.status.value} ({outcome.local_date})")
    drain(loaded, store, definition.profile)


def project_for(loaded: LoadedConfig, profile: str) -> ProjectContext:
    return ProjectContext(loaded, select_profile(loaded.config, profile))
