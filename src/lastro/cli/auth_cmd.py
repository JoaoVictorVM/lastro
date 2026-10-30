"""Grupo lastro auth: diagnóstico de login e tenant sem criar run e sem gravar eventos."""

import asyncio
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

import typer
from playwright.async_api import Error as PlaywrightError

from lastro.auth import (
    AuthError,
    SessionOutcome,
    StorageProtectionError,
    failure_message,
    run_auth_check,
    success_message,
)
from lastro.cli import output
from lastro.cli.state import get_state
from lastro.config.loader import ProjectContext

AUTH_FAILURE_EXIT_CODE = 2
ENVIRONMENT_ERROR_EXIT_CODE = 1
BROWSER_UNAVAILABLE_MESSAGE = (
    "Não foi possível iniciar o Chromium do Playwright. Rode playwright install chromium."
)
STORAGE_ERROR_MESSAGE = "Não foi possível proteger o arquivo de sessão no private data root."

type CheckRunner = Callable[[ProjectContext], Coroutine[Any, Any, SessionOutcome]]


def _default_runner(project: ProjectContext) -> Coroutine[Any, Any, SessionOutcome]:
    return run_auth_check(project)


# Substituível nos testes que não sobem browser.
check_runner: CheckRunner = _default_runner

auth_app = typer.Typer(
    help="Autenticação scriptada e verificação do tenant do perfil.",
    no_args_is_help=True,
)


@auth_app.command("check")
def check(
    ctx: typer.Context,
    profile: Annotated[
        str | None,
        typer.Option("--profile", help="Perfil a verificar (sobrepõe a opção global)."),
    ] = None,
) -> None:
    """Faz login, verifica o tenant e imprime o resultado, sem iniciar uma run."""
    state = get_state(ctx)
    if profile is not None:
        state.profile = profile
    project = state.require_history_project()
    try:
        outcome = asyncio.run(check_runner(project))
    except AuthError as exc:
        output.error(exc.message)
        raise typer.Exit(code=AUTH_FAILURE_EXIT_CODE) from None
    except StorageProtectionError:
        output.error(STORAGE_ERROR_MESSAGE)
        raise typer.Exit(code=ENVIRONMENT_ERROR_EXIT_CODE) from None
    except PlaywrightError:
        output.error(BROWSER_UNAVAILABLE_MESSAGE)
        raise typer.Exit(code=ENVIRONMENT_ERROR_EXIT_CODE) from None
    result = outcome.result
    if not result.is_verified:
        output.error(failure_message(result))
        raise typer.Exit(code=AUTH_FAILURE_EXIT_CODE)
    output.info(success_message(result, project.profile_name))
