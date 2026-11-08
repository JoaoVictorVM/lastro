"""Contratos da reprodução: brief isolado, resultado de tentativa e veredito."""

import hashlib
import json
from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from lastro.history import (
    MAX_REPRODUCTION_NOTE_LENGTH,
    AttemptReason,
    ReproducerResult,
    ReproductionOutcome,
    VerdictInput,
    VerdictReason,
    verdict_label,
)

Verdict = VerdictInput
VerdictLabel = verdict_label

__all__ = [
    "REPRODUCTION_TOOL",
    "AttemptReason",
    "AttemptResult",
    "ReproducerResult",
    "ReproductionBrief",
    "ReproductionResultInput",
    "Verdict",
    "VerdictLabel",
    "VerdictReason",
]


@dataclass(frozen=True)
class ReproductionBrief:
    """Somente o necessário para repetir o defeito: nada do raciocínio nem do observado."""

    flow: str
    entity: str | None
    preconditions: tuple[str, ...]
    steps: tuple[str, ...]
    expected: str
    expected_source: str
    expected_source_ref: str | None

    def as_json(self) -> dict[str, object]:
        return {
            "flow": self.flow,
            "entity": self.entity,
            "preconditions": list(self.preconditions),
            "steps": list(self.steps),
            "expected": {
                "text": self.expected,
                "source": self.expected_source,
                "source_ref": self.expected_source_ref,
            },
        }

    @property
    def sha256(self) -> str:
        canonical = json.dumps(self.as_json(), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def mission_text(self) -> str:
        """Missão da run filha: protocolo de replay e os campos do brief, sem id do finding."""
        lines = [
            "Reproduction attempt. Replay the steps below in this fresh session and report the "
            "outcome with report_reproduction_result. Do not explore beyond these steps and do "
            "not submit findings.",
            "",
            f"Flow: {self.flow}",
        ]
        if self.entity:
            lines.append(f"Entity: {self.entity}")
        lines += ["", "Preconditions (cite the index when one cannot be met):"]
        lines += [f"[{index}] {text}" for index, text in enumerate(self.preconditions)] or ["none"]
        lines += ["", "Steps:"]
        lines += [f"{number}. {text}" for number, text in enumerate(self.steps, start=1)]
        source = self.expected_source + (
            f" ({self.expected_source_ref})" if self.expected_source_ref else ""
        )
        lines += ["", f"Expected result (source: {source}):", self.expected]
        lines += [
            "",
            "Create any data you need with generate_test_data; records from earlier runs are "
            "not available. Capture evidence (screenshot) of what you observe before reporting.",
        ]
        return "\n".join(lines)


@dataclass(frozen=True)
class AttemptResult:
    """Tentativa interpretada pelo servidor; o texto do reprodutor nunca decide o resultado."""

    ordinal: int
    outcome: ReproductionOutcome
    reason: AttemptReason | None
    evidence_artifact_ids: tuple[str, ...] = ()
    missing_precondition: str | None = None
    precondition_index: int | None = None
    note: str = ""

    @property
    def decisive(self) -> bool:
        return self.outcome != ReproductionOutcome.INCONCLUSIVE


REPRODUCTION_TOOL = "report_reproduction_result"


class ReproductionResultInput(BaseModel):
    """Contrato fechado da tool do reprodutor; o servidor valida evidência e índice na run."""

    model_config = ConfigDict(extra="forbid", strict=True)

    result: Annotated[ReproducerResult, Field(strict=False)]
    evidence_artifact_ids: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=64)]], Field(max_length=20)
    ] = Field(default_factory=list[str])
    precondition_index: Annotated[int, Field(ge=0)] | None = None
    note: Annotated[str, Field(max_length=MAX_REPRODUCTION_NOTE_LENGTH)] = ""
