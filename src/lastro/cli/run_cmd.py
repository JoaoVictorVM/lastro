"""Grupo lastro run: cria a run durável antes de qualquer interação com o sistema alvo."""

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from lastro.cli import output
from lastro.cli.state import HISTORY_ERROR_EXIT_CODE, get_state
from lastro.config.loader import ProjectContext
from lastro.executors import get_executor, supported_executors
from lastro.history import (
    MAX_MISSION_TEXT_LENGTH,
    CreatedRun,
    FileMission,
    HistoryError,
    HistoryStore,
    Mission,
    TextMission,
)
from lastro.mission import BundleError, instruction_problems, verify_bundle
from lastro.scaffold.files import executor_spec

USAGE_EXIT_CODE = 2
INTERRUPTED_EXIT_CODE = 130
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


def _bundle_hash(project_dir: Path, executor: str) -> str:
    """Hash do bundle local; sem bundle íntegro e instruções que o referenciem, não há run."""
    try:
        bundle = verify_bundle(project_dir)
    except BundleError as exc:
        output.error(str(exc))
        raise typer.Exit(code=USAGE_EXIT_CODE) from None
    external = executor_spec(executor).external
    if external is None:
        return bundle.sha256
    problems = instruction_problems(project_dir / external.instruction_file, check_length=False)
    if problems:
        output.error(
            f"Instruções do executor inválidas: {'; '.join(problems)}; rode 'lastro init'."
        )
        raise typer.Exit(code=USAGE_EXIT_CODE)
    return bundle.sha256


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
    executor: Annotated[
        str | None,
        typer.Option(
            "--executor",
            help=f"Executor só desta run ({supported_executors()}); padrão: o do lastro.toml.",
        ),
    ] = None,
) -> None:
    """Registra a run, imprime o id e a pasta e entrega o controle ao executor."""
    chosen = mission_from_options(mission, mission_file)
    if chosen is None:
        output.error(MISSION_SOURCE_MESSAGE)
        raise typer.Exit(code=USAGE_EXIT_CODE)
    if executor is not None and get_executor(executor) is None:
        output.error(f"Executor não suportado: {executor} (suportados: {supported_executors()}).")
        raise typer.Exit(code=USAGE_EXIT_CODE)
    state = get_state(ctx)
    project = state.require_history_project()
    if executor is not None:
        project = project.with_executor(executor)
    bundle_hash = _bundle_hash(project.loaded.project_dir, project.config.executor)
    embedded = executor_spec(project.config.executor).embedded
    prepared = _native_preflight(project) if embedded else None
    store = state.open_history(project)
    try:
        created = store.create_run(project, chosen, skill_bundle_version=bundle_hash)
    except HistoryError as exc:
        output.error(str(exc))
        raise typer.Exit(code=HISTORY_ERROR_EXIT_CODE) from None
    output.info(f"Run: {created.record.id}")
    output.info(f"Pasta: {created.paths.run_dir}")
    if prepared is not None:
        raise typer.Exit(code=_run_native(project, store, created, prepared))
    handoff(created)


def _native_preflight(project: ProjectContext) -> Any:
    """Antes de criar a run: extra, chave (por contagem de tokens), modelos e bundle."""
    from lastro.native.runner import PreflightError, preflight

    try:
        return asyncio.run(preflight(project))
    except PreflightError as exc:
        output.error(str(exc))
        raise typer.Exit(code=USAGE_EXIT_CODE) from None


def _run_native(
    project: ProjectContext, store: HistoryStore, created: CreatedRun, prepared: Any
) -> int:
    from lastro.native.runner import NativeRunner, exit_code, summary_lines

    settings, provider = prepared
    runner = NativeRunner(
        project,
        store,
        created.record.id,
        settings,
        provider,
        write=output.info,
        interactive=sys.stdin.isatty(),
    )
    try:
        outcome = asyncio.run(runner.run())
    except KeyboardInterrupt:
        outcome = runner.outcome
        if outcome is not None:
            for line in summary_lines(outcome):
                output.info(line)
        return INTERRUPTED_EXIT_CODE
    for line in summary_lines(outcome):
        output.info(line)
    return exit_code(outcome)


@run_app.command("reverify-tenant")
def reverify_tenant(ctx: typer.Context) -> None:
    """Solicita ao browser dono da run uma nova verificação de tenant."""
    from lastro.browser.control import request_reverification

    state = get_state(ctx)
    project = state.require_history_project()
    history = state.open_history(project)
    run = history.get_active_run()
    if run is None:
        output.error("No active run. Start one with 'lastro run start'.")
        raise typer.Exit(code=USAGE_EXIT_CODE)
    try:
        result = request_reverification(project.project_data_dir, run.id)
    except (OSError, ValueError):
        output.error("Browser owner unavailable; no new browser was started.")
        raise typer.Exit(code=USAGE_EXIT_CODE) from None
    output.info(result)
