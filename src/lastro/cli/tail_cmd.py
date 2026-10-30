"""Comando lastro tail: acompanha os eventos de uma run por cursor de sequência."""

import time
from collections.abc import Callable
from typing import Annotated

import typer

from lastro.cli import output
from lastro.cli.state import HISTORY_ERROR_EXIT_CODE, get_state
from lastro.history import EventRecord, HistoryError, PolicyDecision
from lastro.history.redaction import canonical_json, preview
from lastro.timeutil import to_display_zone

POLL_INTERVAL_SECONDS = 0.5
DESCRIPTION_LENGTH = 80
NO_ACTIVE_RUN_MESSAGE = "Nenhuma run ativa neste projeto. Informe o id: lastro tail <run-id>"
DECISION_COLORS = {
    PolicyDecision.ALLOW: typer.colors.GREEN,
    PolicyDecision.REQUIRE_APPROVAL: typer.colors.YELLOW,
    PolicyDecision.DENY: typer.colors.RED,
}

# Espera entre consultas, substituível nos testes.
sleep: Callable[[float], None] = time.sleep


def describe(event: EventRecord) -> str:
    if event.tool_name is not None:
        text = event.tool_name
        if event.arguments_preview:
            text = f"{text} {event.arguments_preview}"
        return preview(text, DESCRIPTION_LENGTH)
    if event.payload is not None:
        return preview(canonical_json(event.payload), DESCRIPTION_LENGTH)
    return ""


def format_event(event: EventRecord, timezone: str) -> str:
    moment = to_display_zone(event.timestamp, timezone).strftime("%H:%M:%S")
    parts = [moment, event.event_type.value]
    description = describe(event)
    if description:
        parts.append(description)
    if event.policy_decision is not None:
        color = DECISION_COLORS[event.policy_decision]
        parts.append(typer.style(event.policy_decision.value, fg=color, bold=True))
    return "  ".join(parts)


def tail(
    ctx: typer.Context,
    run_id: Annotated[
        str | None, typer.Argument(help="Run a acompanhar; sem id, usa a run ativa.")
    ] = None,
) -> None:
    """Acompanha os eventos da run atual. Ctrl+C encerra apenas o tail."""
    state = get_state(ctx)
    loaded = state.require_history_config()
    store = state.open_history(loaded)
    timezone = loaded.config.timezone
    try:
        if run_id is None:
            active = store.get_active_run()
            if active is None:
                output.error(NO_ACTIVE_RUN_MESSAGE)
                raise typer.Exit(code=HISTORY_ERROR_EXIT_CODE)
            run_id = active.id
        cursor = 0
        while True:
            # O status é lido antes dos eventos: se já era terminal, a leitura seguinte inclui
            # run_ended e esvazia a run sem perder eventos tardios.
            finished = store.get_run(run_id).status.is_terminal
            for event in store.list_events(run_id, after_sequence=cursor):
                output.info(format_event(event, timezone))
                cursor = event.sequence
            if finished:
                return
            sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        output.info("")
        return
    except HistoryError as exc:
        output.error(str(exc))
        raise typer.Exit(code=HISTORY_ERROR_EXIT_CODE) from None
