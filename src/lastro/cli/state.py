"""Opções globais da CLI e a regra de carga de configuração antes dos comandos."""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import typer

from lastro.cli import output
from lastro.config.errors import NOT_EXECUTED_MESSAGE, ConfigError
from lastro.config.loader import (
    ConfigNotFoundError,
    LoadedConfig,
    MissingProjectIdError,
    ProjectContext,
    load_config_file,
    load_project,
    select_profile,
)
from lastro.history import HistoryError, HistoryStore, open_history

CONFIG_ERROR_EXIT_CODE = 2
HISTORY_ERROR_EXIT_CODE = 1


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

    def require_history_project(self) -> ProjectContext:
        """Configuração válida, perfil ativo e project_id persistido, antes de qualquer efeito."""
        project = self.require_project()
        _require_project_id(project)
        return project

    def require_history_config(self) -> LoadedConfig:
        """Para comandos do histórico, que valem para o projeto inteiro e não exigem perfil."""
        try:
            loaded = load_config_file(self.config_path)
            if self.profile is not None:
                select_profile(loaded.config, self.profile)
        except ConfigError as exc:
            report_config_error(exc)
            raise typer.Exit(code=CONFIG_ERROR_EXIT_CODE) from None
        _require_project_id(loaded)
        return loaded

    def open_history(self, source: ProjectContext | LoadedConfig) -> HistoryStore:
        try:
            return open_history(source)
        except HistoryError as exc:
            output.error(str(exc))
            raise typer.Exit(code=HISTORY_ERROR_EXIT_CODE) from None
        except (OSError, sqlite3.Error) as exc:
            output.error(f"Não foi possível abrir o histórico do projeto: {exc}")
            raise typer.Exit(code=HISTORY_ERROR_EXIT_CODE) from None


def _require_project_id(source: ProjectContext | LoadedConfig) -> None:
    try:
        source.require_project_id()
    except MissingProjectIdError as exc:
        for line in exc.lines:
            output.error(line)
        raise typer.Exit(code=CONFIG_ERROR_EXIT_CODE) from None


def get_state(ctx: typer.Context) -> CliState:
    state = ctx.find_object(CliState)
    return state if state is not None else CliState()
