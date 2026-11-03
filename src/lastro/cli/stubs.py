"""Comandos ainda não implementados, registrados para que o --help mostre a árvore final."""

import typer

from lastro.cli import output

STUB_COMMANDS: dict[str, str] = {
    "knowledge": "Gerencia a base de conhecimento do sistema alvo.",
    "eval": "Executa avaliações do agente.",
}

# Argumentos livres permitem que cada comando vire um grupo real depois sem quebrar quem já o chama.
_FREE_ARGS = {"allow_extra_args": True, "ignore_unknown_options": True}


def not_implemented_message(name: str) -> str:
    return f"{name}: não implementado nesta versão"


def _make_stub(name: str):
    def stub(ctx: typer.Context) -> None:
        output.error(not_implemented_message(name))
        raise typer.Exit(code=1)

    stub.__name__ = f"{name}_stub"
    return stub


def register(app: typer.Typer) -> None:
    for name, help_text in STUB_COMMANDS.items():
        app.command(name=name, help=help_text, context_settings=_FREE_ARGS)(_make_stub(name))
