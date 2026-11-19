"""Ragas sobre o juiz isolado da avaliação: precisão de contexto, cobertura e fidelidade.

O Ragas só vê pergunta, contextos realmente entregues, resposta gerada e referência revisada.
Toda chamada de modelo passa pelo `ask` do coordenador, que reserva orçamento, conta inclusive
as repetições internas do Ragas e pausa a suite em limite do provedor. Métrica que falha fica
ausente com motivo; nunca vira zero nem é substituída por comparação de texto.
"""

import asyncio
import importlib
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib import metadata
from typing import Any, Protocol

from lastro.evaluation.models import RETRIEVAL_METRICS, EvalError

# Métrica do Lastro -> classe da versão fixada do Ragas (extra retrieval-eval).
RAGAS_CLASSES = {
    "retrieval.context_precision": "LLMContextPrecisionWithReference",
    "retrieval.context_recall": "LLMContextRecall",
    "retrieval.faithfulness": "Faithfulness",
}
PAUSE_CODES = frozenset({"rate_limit", "usage_cap", "suite_cost_cap"})

type Ask = Callable[[str], str]


@dataclass(frozen=True)
class RetrievalCase:
    question: str
    contexts: tuple[str, ...]
    answer: str
    reference: str


@dataclass(frozen=True)
class MetricOutcome:
    value: float | None
    reason: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {"value": self.value, "reason": self.reason}


class RagasPort(Protocol):
    def identity(self) -> dict[str, Any]: ...

    def score(self, case: RetrievalCase, ask: Ask) -> dict[str, MetricOutcome]: ...


def _module(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except ImportError:
        raise EvalError(
            "ragas_unavailable",
            "Extra retrieval-eval não instalado. Rode uv sync --extra retrieval-eval.",
        ) from None


def valid_score(value: object) -> MetricOutcome:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return MetricOutcome(None, "invalid_score")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        return MetricOutcome(None, "invalid_score")
    return MetricOutcome(number)


def _judge_llm(ask: Ask) -> Any:
    """LLM do Ragas que delega ao juiz; cada geração é uma chamada contabilizada."""
    base: Any = _module("ragas.llms.base").BaseRagasLLM
    outputs: Any = _module("langchain_core.outputs")

    class JudgeLLM(base):  # pyright: ignore[reportUntypedBaseClass]
        def __init__(self) -> None:
            super().__init__()  # pyright: ignore[reportUnknownMemberType]

        def generate_text(
            self,
            prompt: Any,
            n: int = 1,
            temperature: float | None = None,
            stop: list[str] | None = None,
            callbacks: Any = None,
        ) -> Any:
            texts = [ask(str(prompt.to_string())) for _ in range(max(1, n))]
            return outputs.LLMResult(generations=[[outputs.Generation(text=t) for t in texts]])

        async def agenerate_text(
            self,
            prompt: Any,
            n: int = 1,
            temperature: float | None = None,
            stop: list[str] | None = None,
            callbacks: Any = None,
        ) -> Any:
            return self.generate_text(prompt, n, temperature, stop, callbacks)

        def is_finished(self, response: Any) -> bool:
            return True

    return JudgeLLM()


class RagasAdapter:
    """Adaptador para a versão fixada do Ragas; incompatibilidade vira erro explícito."""

    def __init__(self, metrics: Sequence[str] = RETRIEVAL_METRICS) -> None:
        self.metrics = tuple(metrics)

    def identity(self) -> dict[str, Any]:
        try:
            version = metadata.version("ragas")
        except metadata.PackageNotFoundError:
            version = "missing"
        return {
            "name": "ragas",
            "version": version,
            "metrics": {name: RAGAS_CLASSES[name] for name in self.metrics},
        }

    def score(self, case: RetrievalCase, ask: Ask) -> dict[str, MetricOutcome]:
        metrics_module: Any = _module("ragas.metrics")
        sample_type: Any = _module("ragas.dataset_schema").SingleTurnSample
        try:
            sample = sample_type(
                user_input=case.question,
                retrieved_contexts=list(case.contexts),
                response=case.answer,
                reference=case.reference,
            )
            llm = _judge_llm(ask)
        except EvalError:
            raise
        except Exception as exc:
            raise EvalError(
                "ragas_incompatible", f"Ragas instalado é incompatível: {type(exc).__name__}"
            ) from None
        outcomes: dict[str, MetricOutcome] = {}
        for name in self.metrics:
            if not case.contexts and name != "retrieval.faithfulness":
                outcomes[name] = MetricOutcome(None, "no_context")
                continue
            try:
                metric = getattr(metrics_module, RAGAS_CLASSES[name])(llm=llm)
                value = asyncio.run(metric.single_turn_ascore(sample))
            except EvalError as exc:
                if exc.code in PAUSE_CODES:
                    raise
                outcomes[name] = MetricOutcome(None, f"judge_error:{exc.code}")
                continue
            except Exception as exc:
                outcomes[name] = MetricOutcome(None, f"metric_error:{type(exc).__name__}")
                continue
            outcomes[name] = valid_score(value)
        return outcomes
