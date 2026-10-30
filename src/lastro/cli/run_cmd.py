"""Grupo lastro run: cria a run durável antes de qualquer interação com o sistema alvo."""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from lastro.cli import output
from lastro.cli.state import HISTORY_ERROR_EXIT_CODE, get_state
from lastro.history import (
    MAX_MISSION_TEXT_LENGTH,
    CreatedRun,
    FileMission,
    HistoryError,
    Mission,
    TextMission,
)

USAGE_EXIT_CODE = 2
MISSION_SOURCE_MESSAGE = (
    "Informe exatamente uma fonte de missão: --mission TEXTO ou --mission-file ARQUIVO."
)

type Handoff = Callable[[CreatedRun], None]


def _no_handoff(created: CreatedRun) -> None:
    return None


# Ponto de entrega para a sessão do executor. Ela só recebe a run depois que o registro, a
# missão e run_started já estão confirmados no histórico.
handoff: Handoff = _no_handoff


def mission_from_options(text: str | None, file: Path | None) -> Mission | None:
    """As duas fontes são exclusivas; ambas ou nenhuma não formam uma missão."""
    if text is not None and file is None:
        return TextMission(text)
    if file is not None and text is None:
        return FileMission(file)
    return None


run_app = typer.Typer(
    help="Executa uma missão contra o perfil selecionado.",
    no_args_is_help=True,
)


@run_app.command("start")
def start(
    ctx: typer.Context,
    mission: Annotated[
        str | None,
        typer.Option(
            "--mission",
            help=f"Missão em texto (até {MAX_MISSION_TEXT_LENGTH} caracteres).",
        ),
    ] = None,
    mission_file: Annotated[
        Path | None,
        typer.Option("--mission-file", help="Arquivo com a missão (ex.: missions/cadastro.md)."),
    ] = None,
) -> None:
    """Registra a run, imprime o id e a pasta e entrega o controle ao executor."""
    chosen = mission_from_options(mission, mission_file)
    if chosen is None:
        output.error(MISSION_SOURCE_MESSAGE)
        raise typer.Exit(code=USAGE_EXIT_CODE)
    state = get_state(ctx)
    project = state.require_history_project()
    store = state.open_history(project)
    try:
        created = store.create_run(project, chosen)
    except HistoryError as exc:
        output.error(str(exc))
        raise typer.Exit(code=HISTORY_ERROR_EXIT_CODE) from None
    output.info(f"Run: {created.record.id}")
    output.info(f"Pasta: {created.paths.run_dir}")
    handoff(created)
