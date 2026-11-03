"""Compara um fato lembrado com uma observação atual, sem executar nada no alvo.

Só presença de landmarks e atributos estruturais de campos são verificáveis em código. Texto ou
comportamento que o coletor não comprova exige atestado humano.
"""

from typing import Literal, cast

from lastro.knowledge.models import (
    Fact,
    FieldPropertyFact,
    JsonObject,
    JsonValue,
    ScreenFact,
)

type Verdict = Literal["match", "mismatch", "unsupported", "inconclusive"]

STRUCTURAL_ATTRIBUTES = frozenset({"required", "visible", "enabled"})


def _items(projection: JsonObject, key: str) -> list[JsonObject]:
    raw: JsonValue = projection.get(key)
    if not isinstance(raw, list):
        return []
    return [cast(JsonObject, item) for item in raw if isinstance(item, dict)]


def _field_matches(item: JsonObject, fact: FieldPropertyFact) -> bool:
    names = {str(item.get(key) or "").lower() for key in ("name", "id")}
    return fact.field in names or f"{fact.subject}.{fact.field}" in names


def check(fact: Fact, projection: JsonObject) -> Verdict:
    incomplete = bool(projection.get("incomplete"))
    match fact:
        case ScreenFact():
            if projection.get("route") != fact.route:
                return "mismatch"
            seen = {
                (item.get("role"), item.get("label")) for item in _items(projection, "landmarks")
            }
            missing = [mark for mark in fact.landmarks if (mark.role, mark.label) not in seen]
            if not missing:
                return "match"
            # Snapshot incompleto nunca comprova ausência.
            return "inconclusive" if incomplete else "mismatch"
        case FieldPropertyFact():
            if fact.attribute not in STRUCTURAL_ATTRIBUTES:
                return "unsupported"
            fields = [item for item in _items(projection, "fields") if _field_matches(item, fact)]
            if len(fields) > 1:
                return "inconclusive"
            if not fields:
                if fact.attribute == "visible" and fact.value is False and not incomplete:
                    return "match"
                return "inconclusive" if incomplete or fact.attribute != "visible" else "mismatch"
            observed = fields[0].get(fact.attribute)
            if not isinstance(observed, bool):
                return "inconclusive"
            return "match" if observed == fact.value else "mismatch"
        case _:
            return "unsupported"
