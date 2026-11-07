"""Limites de execução por perfil, com os defaults efetivos materializados no snapshot da run."""

from typing import Annotated, cast

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_MAX_MINUTES = 30
DEFAULT_MAX_ACTIONS = 150
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MAX_NO_PROGRESS_ACTIONS = 10

PositiveLimit = Annotated[int, Field(gt=0, strict=True)]


class RunLimits(BaseModel):
    """Tabela `profiles.<nome>.run_limits`: chave desconhecida, bool ou texto falham no load."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    max_minutes: PositiveLimit = DEFAULT_MAX_MINUTES
    max_actions: PositiveLimit = DEFAULT_MAX_ACTIONS
    # Tentativas totais de uma mesma operação recuperável, incluindo a primeira.
    max_attempts: PositiveLimit = DEFAULT_MAX_ATTEMPTS
    max_no_progress_actions: PositiveLimit = DEFAULT_MAX_NO_PROGRESS_ACTIONS

    def snapshot(self) -> dict[str, int]:
        return self.model_dump(mode="json")


def limits_from_snapshot(data: object) -> RunLimits:
    """Limites de um snapshot persistido; run legada recebe só os defaults ausentes, em memória.

    Chave desconhecida ou valor inválido não é reinterpretado silenciosamente: o default vale
    apenas para as chaves válidas ausentes, e qualquer valor inválido usa o default da chave.
    """
    if not isinstance(data, dict):
        return RunLimits()
    table = cast(dict[str, object], data)
    values: dict[str, int] = {}
    for key in RunLimits.model_fields:
        value = table.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            values[key] = value
    return RunLimits(**values)
