"""Reprodução independente por perfil: tentativas, teto por tentativa, modo e pares de executor."""

from typing import Annotated, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

from lastro.config.limits import RunLimits
from lastro.executors import EXECUTORS

DEFAULT_ATTEMPTS = 2
DEFAULT_INTERMITTENT_ATTEMPTS = 3
MAX_ATTEMPTS = 5
MAX_INTERMITTENT_ATTEMPTS = 7
DEFAULT_MAX_MINUTES = 10
DEFAULT_MAX_ACTIONS = 50

type Mode = Literal["same", "cross"]


class ReproductionConfigError(Exception):
    """Par de executores inválido para a run explorada; nenhuma run filha é criada."""

    code = "reproduction_config_invalid"


class ReproductionSettings(BaseModel):
    """Tabela `profiles.<nome>.reproduction`: chave desconhecida, bool ou texto falham no load."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    attempts: Annotated[int, Field(ge=1, le=MAX_ATTEMPTS)] = DEFAULT_ATTEMPTS
    intermittent_attempts: Annotated[int, Field(ge=1, le=MAX_INTERMITTENT_ATTEMPTS)] = (
        DEFAULT_INTERMITTENT_ATTEMPTS
    )
    # Teto de cada tentativa; a run filha recebe o menor entre este valor e o limite do perfil.
    max_minutes: Annotated[int, Field(gt=0)] = DEFAULT_MAX_MINUTES
    max_actions: Annotated[int, Field(gt=0)] = DEFAULT_MAX_ACTIONS
    mode: Mode = "same"
    verifiers: dict[str, str] = Field(default_factory=dict[str, str])
    auto: bool = True

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.intermittent_attempts < self.attempts:
            raise PydanticCustomError(
                "lastro_reproduction_attempts",
                "expected intermittent_attempts greater than or equal to attempts",
            )
        unknown = sorted(
            name for pair in self.verifiers.items() for name in pair if name not in EXECUTORS
        )
        if unknown:
            raise PydanticCustomError(
                "lastro_reproduction_executor",
                "expected executors among {known} (unknown: {names})",
                {"known": ", ".join(EXECUTORS), "names": ", ".join(sorted(set(unknown)))},
            )
        same = sorted(
            explorer for explorer, verifier in self.verifiers.items() if explorer == verifier
        )
        if same:
            raise PydanticCustomError(
                "lastro_reproduction_pair",
                "expected a verifier different from the explorer (same executor: {names})",
                {"names": ", ".join(same)},
            )
        if self.mode == "cross" and not self.verifiers:
            raise PydanticCustomError(
                "lastro_reproduction_cross",
                "expected verifiers (explorer = verifier pairs) when mode is cross",
            )
        return self

    def planned_attempts(self, intermittent: bool) -> int:
        return self.intermittent_attempts if intermittent else self.attempts

    def child_limits(self, profile: RunLimits) -> RunLimits:
        """Orçamento de uma tentativa: nunca maior que o do perfil nem que o teto de reprodução."""
        return profile.model_copy(
            update={
                "max_minutes": min(profile.max_minutes, self.max_minutes),
                "max_actions": min(profile.max_actions, self.max_actions),
            }
        )

    def verifier_for(self, explorer: str) -> str:
        """Executor das tentativas; no modo cross, o par deve existir e diferir do explorador."""
        if self.mode == "same":
            return explorer
        verifier = self.verifiers.get(explorer)
        if verifier is None:
            raise ReproductionConfigError(
                f"No verifier configured for explorer {explorer} "
                f"(set profiles.<name>.reproduction.verifiers.{explorer})."
            )
        if verifier == explorer or verifier not in EXECUTORS:
            raise ReproductionConfigError(
                f"Verifier {verifier} must be a supported executor different from {explorer}."
            )
        return verifier

    def snapshot(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def settings_from_snapshot(data: object, current: ReproductionSettings) -> ReproductionSettings:
    """Valores efetivos congelados na run explorada; run anterior à reprodução usa os atuais."""
    if not isinstance(data, dict):
        return current
    try:
        return ReproductionSettings.model_validate(cast(dict[str, object], data))
    except ValueError:
        return current
