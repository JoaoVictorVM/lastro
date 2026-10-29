"""Opções globais da CLI e a regra de carga de configuração antes dos comandos."""

from dataclasses import dataclass
from pathlib import Path

import typer

from lastro.cli import output
from lastro.config.errors import NOT_EXECUTED_MESSAGE, ConfigError
from lastro.config.loader import (
    ConfigNotFoundError,
    LoadedConfig,
    ProjectContext,
    load_config_file,
    load_project,
)

CONFIG_ERROR_EXIT_CODE = 2


def report_config_error(exc: ConfigError) -> None:
    for line in exc.lines:
        output.error(line)
    output.error(NOT_EXECUTED_MESSAGE)


@dataclass
class CliState:
    profile: str | None = None
    config_path: Path | None = None

    def require_project(self) -> ProjectContext:
        """Para comandos que exigem configuração válida: qualquer erro encerra com código 2."""
        try:
            return load_project(self.config_path, self.profile)
        except ConfigError as exc:
            report_config_error(exc)
            raise typer.Exit(code=CONFIG_ERROR_EXIT_CODE) from None

    def optional_config(self) -> LoadedConfig | None:
        """Para comandos que funcionam sem lastro.toml, mas não com um lastro.toml inválido."""
        try:
            return load_config_file(self.config_path)
        except ConfigNotFoundError as exc:
            if self.config_path is None:
                return None
            report_config_error(exc)
            raise typer.Exit(code=CONFIG_ERROR_EXIT_CODE) from None
        except ConfigError as exc:
            report_config_error(exc)
            raise typer.Exit(code=CONFIG_ERROR_EXIT_CODE) from None


def get_state(ctx: typer.Context) -> CliState:
    state = ctx.find_object(CliState)
    return state if state is not None else CliState()
