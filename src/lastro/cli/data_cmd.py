"""Grupo lastro data: registros criados pelas runs e a limpeza manual feita fora do Lastro."""

from typing import Annotated

import typer

from lastro.cli import output
from lastro.cli.state import HISTORY_ERROR_EXIT_CODE, get_state
from lastro.history import CleanupStatus, CreatedRecord, HistoryError
from lastro.history.redaction import redact_text
from lastro.timeutil import format_for_display

EMPTY_MESSAGE = "Nenhum registro pendente de limpeza."

data_app = typer.Typer(
    help="Registros criados pelas runs e confirmação de limpeza manual.", no_args_is_help=True
)


def record_lines(record: CreatedRecord, timezone: str) -> list[str]:
    lines = [
        f"{record.id}  run {record.run_id}  perfil {record.profile}  tenant {record.tenant}",
        f"  {record.entity_type} {record.system_identifier}  criado em "
        f"{format_for_display(record.created_at, timezone)}",
    ]
    if record.detail_url is not None:
        lines.append(f"  Detalhe: {record.detail_url}")
    if record.cleanup_instructions:
        lines.append(f"  Limpeza: {redact_text(record.cleanup_instructions)}")
    if record.warnings:
        lines.append(f"  Alertas: {', '.join(record.warnings)}")
    return lines


@data_app.command("pending")
def pending(
    ctx: typer.Context,
    profile: Annotated[
        str | None, typer.Option("--profile", help="Somente registros de runs deste perfil.")
    ] = None,
) -> None:
    """Lista os registros com limpeza pendente, do mais antigo ao mais recente."""
    state = get_state(ctx)
    loaded = state.require_history_config()
    store = state.open_history(loaded)
    records = store.list_created_records(
        cleanup_status=CleanupStatus.PENDING, profile=profile or state.profile
    )
    if not records:
        output.info(EMPTY_MESSAGE)
        return
    output.info(f"Registros pendentes de limpeza: {len(records)}")
    for record in records:
        for line in record_lines(record, loaded.config.timezone):
            output.info(line)


@data_app.command("mark-cleaned")
def mark_cleaned(
    ctx: typer.Context,
    tracking_id: Annotated[
        str, typer.Argument(help="Id do registro mostrado por 'lastro data pending'.")
    ],
) -> None:
    """Registra que a limpeza foi feita manualmente; o Lastro não altera o sistema alvo."""
    state = get_state(ctx)
    loaded = state.require_history_config()
    store = state.open_history(loaded)
    try:
        result = store.mark_created_record_cleaned(tracking_id)
    except HistoryError as exc:
        output.error(str(exc))
        raise typer.Exit(code=HISTORY_ERROR_EXIT_CODE) from None
    moment = format_for_display(result.record.cleaned_at or "", loaded.config.timezone)
    if result.changed:
        output.info(f"Registro {tracking_id} marcado como limpo manualmente em {moment}.")
    else:
        output.info(f"Registro {tracking_id} já estava marcado como limpo manualmente em {moment}.")
