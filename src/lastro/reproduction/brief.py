"""Brief de reprodução construído no servidor a partir do registro do finding."""

from lastro.history import Finding
from lastro.reproduction.models import ReproductionBrief

# Lista fechada: qualquer campo novo do finding fica fora até ser incluído aqui de propósito.
BRIEF_FIELDS = (
    "flow",
    "entity",
    "preconditions",
    "steps",
    "expected",
    "expected_source",
    "expected_source_ref",
)


def build_brief(finding: Finding, entity: str | None = None) -> ReproductionBrief:
    """Somente passos, pré-condições e expected; observado, evidência e limitações ficam fora."""
    return ReproductionBrief(
        flow=finding.flow,
        entity=entity,
        preconditions=finding.preconditions,
        steps=finding.steps,
        expected=finding.expected,
        expected_source=finding.expected_source.value,
        expected_source_ref=finding.expected_source_ref,
    )
