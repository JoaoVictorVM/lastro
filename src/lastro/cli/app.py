"""Raiz da CLI: opções globais e registro de todos os comandos."""

import platform
from pathlib import Path
from typing import Annotated

import typer

from lastro import get_version
from lastro.cli import demo_cmd, doctor_cmd, init_cmd, stubs
from lastro.cli.state import CliState

app = typer.Typer(
    name="lastro",
    help="Lastro: exploração de sistemas web com políticas, histórico e evidências.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)


def version_line() -> str:
    python_version = platform.python_version()
    return f"lastro {get_version()} (python {python_version}, {platform.platform(terse=True)})"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(version_line())
        raise typer.Exit()


@app.callback()
def root(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Mostra a versão instalada e sai.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
    profile: Annotated[
        str | None,
        typer.Option("--profile", help="Perfil de ambiente a usar (obrigatório com vários)."),
    ] = None,
    config: Annotated[
        Path | None,
        typer.Option("--config", help="Caminho explícito do lastro.toml."),
    ] = None,
) -> None:
    ctx.obj = CliState(profile=profile, config_path=config)


app.command(name="init")(init_cmd.init)
app.command(name="doctor")(doctor_cmd.doctor)
app.add_typer(demo_cmd.demo_app, name="demo")
stubs.register(app)


def main() -> None:
    app()
