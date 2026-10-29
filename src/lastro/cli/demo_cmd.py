"""Grupo lastro demo: sobe e para o demo local delegando ao provedor do demo."""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from lastro.cli import output
from lastro.cli.demo_runner_port import DemoProviderError, DemoRunnerPort, load_demo_runner
from lastro.cli.state import get_state
from lastro.config.loader import LoadedConfig
from lastro.config.schema import DEFAULT_DEMO_PORT, DEMO_PORT_MAX, DEMO_PORT_MIN
from lastro.paths import default_private_data_root, demo_state_dir

DEMO_FAILURE_EXIT_CODE = 1

demo_app = typer.Typer(
    name="demo",
    help="Demo local para avaliar o lastro sem um sistema privado.",
    no_args_is_help=True,
)

runner_loader: Callable[[], DemoRunnerPort] = load_demo_runner

PortOption = Annotated[
    int | None,
    typer.Option(
        "--port",
        min=DEMO_PORT_MIN,
        max=DEMO_PORT_MAX,
        help=f"Porta do demo (padrão: demo.port do lastro.toml ou {DEFAULT_DEMO_PORT}).",
    ),
]


def resolve_port(flag: int | None, loaded: LoadedConfig | None) -> int:
    if flag is not None:
        return flag
    if loaded is not None:
        return loaded.config.demo.port
    return DEFAULT_DEMO_PORT


def resolve_state_dir(loaded: LoadedConfig | None) -> Path:
    root = loaded.private_data_root if loaded is not None else default_private_data_root()
    return demo_state_dir(root)


def _fail(message: str) -> typer.Exit:
    output.error(message)
    return typer.Exit(code=DEMO_FAILURE_EXIT_CODE)


def _prepare(ctx: typer.Context) -> tuple[LoadedConfig | None, DemoRunnerPort, Path]:
    loaded = get_state(ctx).optional_config()
    try:
        runner = runner_loader()
    except DemoProviderError as exc:
        raise _fail(str(exc)) from None
    state_dir = resolve_state_dir(loaded)
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _fail(f"Não foi possível criar {state_dir}: {exc.strerror or exc}") from None
    return loaded, runner, state_dir


@demo_app.command("up")
def up(ctx: typer.Context, port: PortOption = None) -> None:
    """Sobe o demo local em 127.0.0.1."""
    loaded, runner, state_dir = _prepare(ctx)
    try:
        outcome = runner.start(state_dir, resolve_port(port, loaded))
    except DemoProviderError as exc:
        raise _fail(f"Falha ao subir o demo: {exc}") from None
    output.info(outcome.ready_message)
    output.info(f"URL: {outcome.url}")
    if outcome.credential_env_vars:
        output.info("Variáveis de credencial dos logins de teste:")
        for name in outcome.credential_env_vars:
            output.info(f"  {name}")


@demo_app.command("down")
def down(ctx: typer.Context, port: PortOption = None) -> None:
    """Para o demo local, se estiver rodando."""
    _, runner, state_dir = _prepare(ctx)
    try:
        outcome = runner.stop(state_dir)
    except DemoProviderError as exc:
        raise _fail(f"Falha ao parar o demo: {exc}") from None
    if outcome.message:
        output.info(outcome.message)
    elif outcome.was_running:
        output.info("Demo parado.")
    else:
        output.info("O demo não estava rodando.")
