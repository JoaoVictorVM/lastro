"""Statement por template, chave factual e valor canônico de cada tipo de fato.

Nada aqui copia blocos livres da página: o texto exibido sai do fato tipado.
"""

import hashlib
import json
from dataclasses import dataclass

from lastro.knowledge.models import (
    MAX_STATEMENT_LENGTH,
    ClarificationFact,
    ExperienceFact,
    Fact,
    FieldPropertyFact,
    FlowStepFact,
    JsonObject,
    JsonValue,
    ScreenFact,
)


def canonical(value: JsonValue) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def digest(value: JsonValue) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def fact_json(fact: Fact) -> JsonObject:
    return fact.model_dump(mode="json")


def _value(value: bool | str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def render_statement(fact: Fact) -> str:
    """Texto curto e determinístico; citações da página aparecem entre aspas."""
    match fact:
        case FieldPropertyFact():
            text = f"{fact.subject}.{fact.field}: {fact.attribute}={_value(fact.value)}"
        case ScreenFact():
            marks = ", ".join(f'{item.role} "{item.label}"' for item in fact.landmarks)
            text = f"screen {fact.route}" + (f" has {marks}" if marks else "")
        case FlowStepFact():
            text = f"{fact.action} from {fact.origin} leads to {fact.destination}"
        case ClarificationFact():
            answer = fact.answer if fact.answer is not None else "(pending)"
            text = f"{fact.subject} [{fact.gap_id}]: {answer}"
        case ExperienceFact():
            text = f"{fact.attempt} at {fact.context.route}: {fact.outcome}"
            if fact.message is not None:
                text += f' ("{fact.message}")'
    if len(text) > MAX_STATEMENT_LENGTH:
        raise ValueError("statement exceeds 1000 characters")
    return text


@dataclass(frozen=True)
class FactIdentity:
    """Chave comparável e hash do restante do fato, base da deduplicação e do conflito."""

    key: str
    value: JsonValue
    value_sha256: str


def identity(fact: Fact) -> FactIdentity:
    data = fact_json(fact)
    match fact:
        case FieldPropertyFact():
            key = f"field_property:{fact.subject}.{fact.field}.{fact.attribute}"
            value: JsonValue = data["value"]
        case ScreenFact():
            key = f"screen:{fact.route}"
            marks: list[JsonValue] = [
                mark
                for mark in sorted(
                    canonical(item.model_dump(mode="json")) for item in fact.landmarks
                )
            ]
            value = marks
        case FlowStepFact():
            key = f"flow_step:{fact.origin}:{fact.action}"
            value = fact.destination
        case ClarificationFact():
            key = f"clarification:{fact.gap_id.lower()}:{fact.subject}"
            value = fact.answer
        case ExperienceFact():
            # Tentativas distintas nunca viram regras conflitantes entre si.
            key = f"experience:{fact.context.observation_id}:{fact.attempt}"
            value = {k: v for k, v in data.items() if k not in {"type", "attempt"}}
    return FactIdentity(key=key, value=value, value_sha256=digest(value))


def search_text(flow: str, fact: Fact) -> str:
    """Projeção indexável permitida: chaves e citações, sem payload bruto."""
    parts: list[str] = [flow, fact.type]
    match fact:
        case FieldPropertyFact():
            parts += [fact.subject, fact.field, fact.attribute, _value(fact.value)]
        case ScreenFact():
            parts.append(fact.route)
            for item in fact.landmarks:
                parts += [item.role, item.label]
        case FlowStepFact():
            parts += [fact.origin, fact.destination, fact.action]
        case ClarificationFact():
            parts += [fact.gap_id, fact.subject, fact.answer or ""]
        case ExperienceFact():
            parts += [fact.attempt, fact.outcome, fact.context.route, fact.message or ""]
    return " ".join(part for part in parts if part)


def quoted_texts(fact: Fact) -> list[str]:
    """Todo texto livre do fato, alvo da inspeção de diretivas."""
    match fact:
        case ScreenFact():
            return [item.label for item in fact.landmarks]
        case ClarificationFact():
            return [fact.answer] if fact.answer else []
        case ExperienceFact():
            return [fact.message] if fact.message else []
        case _:
            return []


def routes(fact: Fact) -> tuple[str, ...]:
    """Rotas citadas, conferidas contra as observações reais da run."""
    match fact:
        case ScreenFact():
            return (fact.route,)
        case FlowStepFact():
            return (fact.origin, fact.destination)
        case ExperienceFact():
            return (fact.context.route,)
        case _:
            return ()
