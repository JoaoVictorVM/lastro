"""Comando lastro approve: decisão humana de um gate, em qualquer terminal e fora do modelo."""

from typing import Annotated

import typer

from lastro.approvals import request_lines
from lastro.cli import output
from lastro.cli.state import HISTORY_ERROR_EXIT_CODE, get_state
from lastro.history import (
    ApprovalChoice,
    ApprovalError,
    ApprovalNotPendingError,
    ApprovalStatus,
    DecidedBy,
    HistoryError,
)
from lastro.history.approvals import END_RUN_REASON
from lastro.timeutil import to_display_zone

USAGE_EXIT_CODE = 2
EXCLUSIVE_MESSAGE = "Use --skip or --end, not both."
VERB = {
    ApprovalChoice.APPROVE: "approved",
    ApprovalChoice.SKIP: "skipped",
    ApprovalChoice.END_RUN: "run ended",
}


def approve(
    ctx: typer.Context,
    action_id: Annotated[str, typer.Argument(help="Id do pedido, como aparece na notificação.")],
    skip: Annotated[
        bool, typer.Option("--skip", help="Recusa a ação; a run continua sem executá-la.")
    ] = False,
    end: Annotated[
        bool, typer.Option("--end", help="Encerra a run como cancelled e gera relatório parcial.")
    ] = False,
    note: Annotated[
        str | None, typer.Option("--note", help="Observação opcional (até 2000 caracteres).")
    ] = None,
) -> None:
    """Aprova, recusa ou encerra a run em um gate de aprovação pendente."""
    if skip and end:
        output.error(EXCLUSIVE_MESSAGE)
        raise typer.Exit(code=USAGE_EXIT_CODE)
    choice = (
        ApprovalChoice.END_RUN if end else ApprovalChoice.SKIP if skip else ApprovalChoice.APPROVE
    )
    state = get_state(ctx)
    loaded = state.require_history_config()
    store = state.open_history(loaded)
    timezone = loaded.config.timezone
    try:
        current = store.get_approval(action_id)
        if current is not None and current.status == ApprovalStatus.PENDING:
            expires = to_display_zone(current.expires_at, timezone).strftime("%H:%M")
            for line in request_lines(current, expires):
                output.info(line)
        record = store.decide_approval(action_id, choice, DecidedBy.CLI, note=note)
    except ApprovalNotPendingError as exc:
        output.error(str(exc))
        raise typer.Exit(code=HISTORY_ERROR_EXIT_CODE) from None
    except ApprovalError as exc:
        output.error(exc.detail)
        raise typer.Exit(code=USAGE_EXIT_CODE) from None
    except HistoryError as exc:
        output.error(str(exc))
        raise typer.Exit(code=HISTORY_ERROR_EXIT_CODE) from None
    moment = to_display_zone(record.decided_at or record.requested_at, timezone).strftime("%H:%M")
    output.info(f"{VERB[choice]} by user at {moment}: {record.summary}")
    if choice == ApprovalChoice.APPROVE:
        output.info("The agent retries the same action once; any change requires a new approval.")
        return
    if choice == ApprovalChoice.SKIP:
        output.info("The action was not executed; the run continues.")
        return
    from lastro.reporting.builder import ReportBuilder

    output.info(f"Run {record.run_id}: cancelled; no further writes.")
    try:
        report = ReportBuilder(store).build_partial(record.run_id, END_RUN_REASON)
    except Exception as exc:
        output.error(f"Partial report failed: {exc}")
        raise typer.Exit(code=HISTORY_ERROR_EXIT_CODE) from None
    path = report.report_path or report.fallback_path
    output.info(f"Partial report: {path}")
