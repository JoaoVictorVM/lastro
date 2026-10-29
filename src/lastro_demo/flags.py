"""Registro fechado das flags de defeito e de cenário e do conjunto ativo em memória."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

DEFECT_IDS: tuple[str, ...] = tuple(f"d{number:02d}" for number in range(1, 11))
SCENARIO_IDS: tuple[str, ...] = ("s01", "s02")
ALL_FLAG_IDS: tuple[str, ...] = DEFECT_IDS + SCENARIO_IDS
VARIANTS: tuple[str, ...] = ("base",)
DEFAULT_VARIANT = "base"

DEFECTS_ENV = "LASTRO_DEMO_DEFECTS"
SCENARIOS_ENV = "LASTRO_DEMO_SCENARIOS"
VARIANT_ENV = "LASTRO_DEMO_VARIANT"

ALL_KEYWORD = "all"
NONE_KEYWORD = "none"


class UnknownFlagError(ValueError):
    def __init__(self, unknown: Iterable[str], known: Iterable[str]) -> None:
        self.unknown = tuple(unknown)
        self.known = tuple(known)
        super().__init__(
            f"Flag desconhecida: {', '.join(self.unknown)}. "
            f"Valores aceitos: {', '.join(self.known)}"
        )


class UnknownVariantError(ValueError):
    def __init__(self, variant: str) -> None:
        self.variant = variant
        super().__init__(
            f"Variante desconhecida: {variant}. Valores aceitos: {', '.join(VARIANTS)}"
        )


def _validate(ids: Iterable[str], known: tuple[str, ...]) -> frozenset[str]:
    normalized = [item.strip().lower() for item in ids]
    unknown = [item for item in normalized if item not in known]
    if unknown:
        raise UnknownFlagError(unknown, known)
    return frozenset(normalized)


def validate_defects(ids: Iterable[str]) -> frozenset[str]:
    return _validate(ids, DEFECT_IDS)


def validate_scenarios(ids: Iterable[str]) -> frozenset[str]:
    return _validate(ids, SCENARIO_IDS)


def validate_variant(variant: str) -> str:
    if variant not in VARIANTS:
        raise UnknownVariantError(variant)
    return variant


def parse_flag_list(raw: str | None, known: tuple[str, ...]) -> frozenset[str]:
    """Interpreta a lista vinda do ambiente: ausente ou "all" ativa tudo; vazia ou "none", nada."""
    if raw is None:
        return frozenset(known)
    tokens = [token for token in raw.replace(";", ",").replace(" ", ",").split(",") if token]
    lowered = [token.strip().lower() for token in tokens]
    if lowered == [ALL_KEYWORD]:
        return frozenset(known)
    if not lowered or lowered == [NONE_KEYWORD]:
        return frozenset()
    return _validate(lowered, known)


@dataclass(frozen=True)
class FlagState:
    defects: frozenset[str]
    scenarios: frozenset[str]
    variant: str = DEFAULT_VARIANT

    @classmethod
    def all_on(cls) -> "FlagState":
        return cls(frozenset(DEFECT_IDS), frozenset(SCENARIO_IDS))

    @classmethod
    def all_off(cls) -> "FlagState":
        return cls(frozenset(), frozenset())

    @classmethod
    def only(cls, *ids: str) -> "FlagState":
        defects = validate_defects(item for item in ids if item in DEFECT_IDS)
        scenarios = validate_scenarios(item for item in ids if item not in DEFECT_IDS)
        return cls(defects, scenarios)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "FlagState":
        return cls(
            defects=parse_flag_list(env.get(DEFECTS_ENV), DEFECT_IDS),
            scenarios=parse_flag_list(env.get(SCENARIOS_ENV), SCENARIO_IDS),
            variant=validate_variant(env.get(VARIANT_ENV) or DEFAULT_VARIANT),
        )

    def defect(self, flag_id: str) -> bool:
        return flag_id in self.defects

    def scenario(self, flag_id: str) -> bool:
        return flag_id in self.scenarios

    def sorted_defects(self) -> list[str]:
        return sorted(self.defects)

    def sorted_scenarios(self) -> list[str]:
        return sorted(self.scenarios)
