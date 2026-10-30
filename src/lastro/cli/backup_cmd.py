"""Comando lastro backup: archive consistente do histórico e das pastas das runs."""

from pathlib import Path
from typing import Annotated

import typer

from lastro.cli import output
from lastro.cli.state import HISTORY_ERROR_EXIT_CODE, get_state
from lastro.history import HistoryError


def backup(
    ctx: typer.Context,
    dest: Annotated[
        Path,
        typer.Argument(help="Arquivo .zip a criar ou pasta existente que receberá o archive."),
    ],
) -> None:
    """Gera um archive com history.sqlite, as pastas das runs e manifest.json."""
    state = get_state(ctx)
    loaded = state.require_history_config()
    store = state.open_history(loaded)
    try:
        result = store.backup(dest)
    except (HistoryError, OSError) as exc:
        output.error(f"Backup não criado: {exc}")
        raise typer.Exit(code=HISTORY_ERROR_EXIT_CODE) from None
    output.info(f"Backup criado: {result.archive}")
    output.info(f"Arquivos no manifesto: {len(result.files)}")
