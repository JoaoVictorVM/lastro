"""Comando lastro tail: acompanha os eventos de uma run por cursor de sequência."""

import re
import sys
import time
from collections.abc import Callable
from typing import Annotated, cast

import typer

from lastro.cli import output
from lastro.cli.state import HISTORY_ERROR_EXIT_CODE, get_state
from lastro.history import (
    ApprovalStatus,
    EventRecord,
    EventType,
    HistoryError,
    HistoryStore,
    PolicyDecision,
)
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

BANNER = "==== APPROVAL REQUIRED ===="
BELL = "\a"
DECIDERS = {
    "cli": "user",
    "executor_prompt": "user",
    "scripted": "scripted approver",
    "system": "system",
}

# Espera entre consultas, substituível nos testes.
sleep: Callable[[float], None] = time.sleep


def is_tty() -> bool:
    return sys.stdout.isatty()


def _field(payload: object, key: str) -> str:
    value: object = cast(dict[str, object], payload).get(key) if isinstance(payload, dict) else None
    return terminal_text(value) if value is not None else ""


def approval_banner(event: EventRecord, timezone: str) -> list[str]:
    """Pedido redigido em destaque: resumo, comando de decisão e expiração."""
    payload = event.payload
    expires = _field(payload, "expires_at")
    until = f" (expires {to_display_zone(expires, timezone):%H:%M})" if expires else ""
    return [
        typer.style(BANNER, fg=typer.colors.YELLOW, bold=True),
        "  " + _field(payload, "summary"),
        f"  Decide: {_field(payload, 'command')} [--skip | --end]{until}",
    ]


def approval_outcome(event: EventRecord, timezone: str) -> str:
    payload = event.payload
    status = _field(payload, "status")
    moment = to_display_zone(event.timestamp, timezone).strftime("%H:%M")
    who = DECIDERS.get(_field(payload, "decided_by"), "user")
    action = _field(payload, "action_id")
    texts = {
        ApprovalStatus.APPROVED.value: f"approved by {who} at {moment}",
        ApprovalStatus.SKIPPED.value: f"skipped by {who} at {moment}",
        ApprovalStatus.EXPIRED.value: f"skipped (no response) at {moment}",
        ApprovalStatus.ENDED.value: f"run ended by {who} at {moment}",
        ApprovalStatus.INVALIDATED.value: (
            f"approval invalidated at {moment}: {_field(payload, 'changed_field')} changed"
        ),
        ApprovalStatus.CONSUMED.value: f"approved action executed once at {moment}",
        ApprovalStatus.EXECUTION_FAILED.value: (
            f"approved action failed at {moment} ({_field(payload, 'error_code')}); verify manually"
        ),
    }
    return f"{texts.get(status, status)} ({action})"


def degraded_line(event: EventRecord) -> str:
    channel = _field(event.payload, "channel")
    code = _field(event.payload, "error_code")
    return f"notification degraded ({channel}: {code}); watch this tail for approvals"


def still_pending(store: HistoryStore, event: EventRecord) -> bool:
    action_id = _field(event.payload, "action_id")
    record = store.get_approval(action_id, event.run_id) if action_id else None
    return record is not None and record.status == ApprovalStatus.PENDING


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


def native_line(event: EventRecord) -> str | None:
    """Eventos do executor nativo em uma linha: nó, tokens e custo, sem conteúdo do modelo."""
    payload = event.payload
    if not isinstance(payload, dict):
        return None
    node = _field(payload, "node")
    attempt = _field(payload, "attempt")
    if event.event_type == EventType.MODEL_CALL_STARTED:
        reserved = _field(payload, "reserved_cost_usd")
        return f"model {node} attempt {attempt} sent (reserved USD {reserved})"
    if event.event_type == EventType.MODEL_CALL_COMPLETED:
        tools = payload.get("tool_calls")
        names = ", ".join(
            terminal_text(item.get("name"))
            for item in (tools if isinstance(tools, list) else [])
            if isinstance(item, dict)
        )
        usage = payload.get("usage")
        return (
            f"model {node} in {_field(usage, 'total_input')} "
            f"out {_field(usage, 'output')} USD {_field(payload, 'cost_usd')} "
            f"{_field(payload, 'latency_ms')} ms" + (f" -> {names}" if names else "")
        )
    if event.event_type == EventType.MODEL_CALL_FAILED:
        return (
            f"model {node} attempt {attempt} failed ({_field(payload, 'error_code')}); "
            f"usage {_field(payload, 'usage_status')}"
        )
    if event.event_type == EventType.AGENT_FAILURE:
        return f"agent failure {_field(payload, 'code')} {_field(payload, 'tool_name')}".rstrip()
    if event.event_type == EventType.NATIVE_STARTED:
        return (
            f"native executor started (catalog {_field(payload, 'catalog_sha256')[:12]}, "
            f"bundle {_field(payload, 'skill_bundle_sha256')[:12]})"
        )
    return None


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
    if event.event_type == EventType.APPROVAL_REQUESTED:
        return "\n".join(f"{moment}  {line}" for line in approval_banner(event, timezone))
    if event.event_type == EventType.APPROVAL_DECIDED:
        return f"{moment}  approval_decided  {approval_outcome(event, timezone)}"
    if event.event_type == EventType.NOTIFICATION_DEGRADED:
        return f"{moment}  {degraded_line(event)}"
    native = native_line(event)
    if native is not None:
        return f"{moment}  {native}"
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
                # Sino só para pedido ainda aberto: rever o histórico não dispara alertas antigos.
                if (
                    event.event_type == EventType.APPROVAL_REQUESTED
                    and is_tty()
                    and still_pending(store, event)
                ):
                    typer.echo(BELL, nl=False)
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
