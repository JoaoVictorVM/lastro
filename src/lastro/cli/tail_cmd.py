"""Comando lastro tail: acompanha os eventos de uma run por cursor de sequência."""

import re
import time
from collections.abc import Callable
from typing import Annotated

import typer

from lastro.cli import output
from lastro.cli.state import HISTORY_ERROR_EXIT_CODE, get_state
from lastro.history import EventRecord, EventType, HistoryError, PolicyDecision
from lastro.history.redaction import canonical_json, preview, redact_text
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


def terminal_text(value: object) -> str:
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", str(value))
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return "".join(c if c.isprintable() else " " for c in redact_text(text))


def oracle_description(event: EventRecord) -> str | None:
    payload = event.payload
    if not isinstance(payload, dict) or "signal_type" not in payload:
        return None
    kind = terminal_text(payload["signal_type"])
    path = terminal_text(payload.get("url_path", ""))
    method = terminal_text(payload.get("http_method") or "")
    status = payload.get("http_status")
    detail = (
        f"-> {terminal_text(status)}"
        if status is not None
        else preview(terminal_text(payload.get("message", "")), 256)
    )
    sequence = payload.get("action_sequence")
    action = (
        f"after action #{terminal_text(sequence)}"
        if sequence is not None
        else ("no action (background)")
    )
    count = payload.get("occurrence_count", 1)
    return f"ORACLE {kind} {method} {path} {detail} ({action}) occurrences: {terminal_text(count)}"


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
    if event.event_type == EventType.ORACLE_SIGNAL:
        description = oracle_description(event)
        if description is not None:
            return f"{moment}  {description}"
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
