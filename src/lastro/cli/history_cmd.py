"""Grupo lastro history: consulta o histórico de runs com filtros combinados por AND."""

import re
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

import typer

from lastro.cli import output
from lastro.cli.state import get_state
from lastro.history import DEFAULT_HISTORY_LIMIT, RunFilter, RunStatus, RunSummary
from lastro.timeutil import parse_timestamp, to_display_zone, utc_now

_RELATIVE_SINCE = re.compile(r"^(\d+)([dhm])$")
_SINCE_UNITS = {"d": "days", "h": "hours", "m": "minutes"}
EMPTY_MESSAGE = "Nenhuma run encontrada."
HEADERS = ("RUN", "INÍCIO", "DURAÇÃO", "STATUS", "PERFIL", "FLUXOS", "FINDINGS C/S/I", "LIMPEZA")

# Relógio substituível para que filtros relativos sejam testáveis.
clock: Callable[[], datetime] = utc_now


def parse_since(value: str, now: datetime, timezone: str) -> datetime:
    """Aceita duração relativa (7d, 12h, 30m) ou data/horário ISO no fuso configurado."""
    match = _RELATIVE_SINCE.match(value.strip())
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        return now - timedelta(**{_SINCE_UNITS[unit]: amount})
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        raise typer.BadParameter(
            "use uma duração como 7d, 12h ou 30m, ou uma data ISO como 2026-10-01"
        ) from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo(timezone))
    return moment


def _parse_status(value: str | None) -> RunStatus | None:
    if value is None:
        return None
    try:
        return RunStatus(value)
    except ValueError:
        allowed = ", ".join(status.value for status in RunStatus)
        raise typer.BadParameter(f"status desconhecido (use um de: {allowed})") from None


def format_duration(summary: RunSummary) -> str:
    record = summary.record
    if record.ended_at is None:
        return "em andamento"
    elapsed = parse_timestamp(record.ended_at) - parse_timestamp(record.started_at)
    seconds = int(elapsed.total_seconds())
    hours, rest = divmod(max(seconds, 0), 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def summary_row(summary: RunSummary, timezone: str) -> tuple[str, ...]:
    record = summary.record
    started = to_display_zone(record.started_at, timezone).strftime("%Y-%m-%d %H:%M:%S")
    findings = summary.findings
    return (
        record.id,
        started,
        format_duration(summary),
        record.status.value,
        record.profile,
        ", ".join(summary.flows) or "-",
        f"{findings.confirmed}/{findings.suspected}/{findings.inconclusive}",
        str(summary.pending_cleanup),
    )


def format_table(summaries: Sequence[RunSummary], timezone: str) -> list[str]:
    if not summaries:
        return [EMPTY_MESSAGE]
    rows = [HEADERS, *(summary_row(summary, timezone) for summary in summaries)]
    widths = [max(len(row[index]) for row in rows) for index in range(len(HEADERS))]
    return [
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in rows
    ]


history_app = typer.Typer(help="Consulta o histórico de runs.", no_args_is_help=True)


@history_app.command("list")
def list_runs(
    ctx: typer.Context,
    flow: Annotated[str | None, typer.Option("--flow", help="Somente runs com este fluxo.")] = None,
    status: Annotated[
        str | None, typer.Option("--status", help="Somente runs neste status (ex.: blocked).")
    ] = None,
    issue: Annotated[
        str | None, typer.Option("--issue", help="Número da issue (ex.: 42) ou id do snapshot.")
    ] = None,
    profile: Annotated[
        str | None, typer.Option("--profile", help="Somente runs deste perfil.")
    ] = None,
    since: Annotated[
        str | None,
        typer.Option("--since", help="Início mínimo: duração (7d, 12h, 30m) ou data ISO."),
    ] = None,
    limit: Annotated[
        int, typer.Option("--limit", min=1, help="Quantidade máxima de runs.")
    ] = DEFAULT_HISTORY_LIMIT,
) -> None:
    """Lista as runs mais recentes primeiro."""
    selected_status = _parse_status(status)
    state = get_state(ctx)
    loaded = state.require_history_config()
    timezone = loaded.config.timezone
    run_filter = RunFilter(
        flow=flow,
        status=selected_status,
        issue=issue,
        profile=profile,
        since=parse_since(since, clock(), timezone) if since is not None else None,
        limit=limit,
    )
    store = state.open_history(loaded)
    for line in format_table(store.list_runs(run_filter), timezone):
        output.info(line)
