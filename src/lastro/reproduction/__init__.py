"""Reprodução independente de findings em runs filhas, sem o raciocínio do explorador."""

from lastro.reproduction.brief import build_brief
from lastro.reproduction.decide import decide
from lastro.reproduction.interpret import AttemptFacts, interpret
from lastro.reproduction.models import (
    REPRODUCTION_TOOL,
    AttemptResult,
    ReproductionBrief,
    ReproductionResultInput,
)

__all__ = [
    "REPRODUCTION_TOOL",
    "AttemptFacts",
    "AttemptResult",
    "ReproductionBrief",
    "ReproductionResultInput",
    "build_brief",
    "decide",
    "interpret",
]
