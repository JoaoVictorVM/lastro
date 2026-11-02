"""Charter canônico construído pelo Python a partir dos campos estruturados."""

from dataclasses import dataclass

from lastro.mission.models import CharterInput, Heuristic

DEFAULT_TIMEBOX_MINUTES = 20
# Sem limite configurado na run, o time box segue o limite padrão de 30 minutos do produto.
DEFAULT_MAX_TIMEBOX_MINUTES = 30


@dataclass(frozen=True)
class Charter:
    statement: str
    target: str
    resources: tuple[str, ...]
    information: str
    timebox_minutes: int
    heuristics: tuple[Heuristic, ...]

    def as_json(self) -> dict[str, object]:
        return {
            "statement": self.statement,
            "target": self.target,
            "resources": list(self.resources),
            "information": self.information,
            "timebox_minutes": self.timebox_minutes,
            "heuristics": [heuristic.value for heuristic in self.heuristics],
        }


def timebox_limit(run_limits: dict[str, object]) -> int:
    value = run_limits.get("max_minutes")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return DEFAULT_MAX_TIMEBOX_MINUTES


def build_charter(data: CharterInput, max_minutes: int = DEFAULT_MAX_TIMEBOX_MINUTES) -> Charter:
    if data.timebox_minutes > max_minutes:
        raise ValueError(
            f"timebox_minutes {data.timebox_minutes} exceeds the run limit of {max_minutes}"
        )
    statement = (
        f"Explore {data.target} with {', '.join(data.resources)} to discover {data.information}"
    )
    return Charter(
        statement=statement,
        target=data.target,
        resources=tuple(data.resources),
        information=data.information,
        timebox_minutes=data.timebox_minutes,
        heuristics=tuple(data.heuristics),
    )
