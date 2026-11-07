"""Charter canônico construído pelo Python a partir dos campos estruturados."""

from dataclasses import dataclass

from lastro.config.limits import DEFAULT_MAX_MINUTES, RunLimits, limits_from_snapshot
from lastro.mission.models import AffectedFlow, CharterInput, Heuristic

DEFAULT_TIMEBOX_MINUTES = 20
# Sem limite configurado na run, o time box segue o limite padrão de 30 minutos do produto.
DEFAULT_MAX_TIMEBOX_MINUTES = DEFAULT_MAX_MINUTES


@dataclass(frozen=True)
class Charter:
    statement: str
    target: str
    resources: tuple[str, ...]
    information: str
    timebox_minutes: int
    heuristics: tuple[Heuristic, ...]
    # Fluxos potencialmente afetados, em ordem de prioridade: hipóteses, não critérios.
    priority_flows: tuple[str, ...] = ()

    def as_json(self) -> dict[str, object]:
        data: dict[str, object] = {
            "statement": self.statement,
            "target": self.target,
            "resources": list(self.resources),
            "information": self.information,
            "timebox_minutes": self.timebox_minutes,
            "heuristics": [heuristic.value for heuristic in self.heuristics],
        }
        # Planos sem fluxos afetados mantêm o mesmo JSON (e hash) de antes.
        if self.priority_flows:
            data["priority_flows"] = list(self.priority_flows)
        return data


def timebox_limit(run_limits: RunLimits | object) -> int:
    """Duração máxima pelo contrato tipado; snapshots legados normalizam só em memória."""
    if isinstance(run_limits, RunLimits):
        return run_limits.max_minutes
    return limits_from_snapshot(run_limits).max_minutes


def prioritized(flows: list[AffectedFlow]) -> tuple[str, ...]:
    ordered = sorted(enumerate(flows), key=lambda item: (item[1].priority, item[0]))
    seen: list[str] = []
    for _, flow in ordered:
        if flow.flow not in seen:
            seen.append(flow.flow)
    return tuple(seen)


def build_charter(
    data: CharterInput,
    max_minutes: int = DEFAULT_MAX_TIMEBOX_MINUTES,
    affected_flows: list[AffectedFlow] | None = None,
) -> Charter:
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
        priority_flows=prioritized(affected_flows or []),
    )
