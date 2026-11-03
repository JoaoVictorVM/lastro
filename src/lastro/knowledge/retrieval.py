"""Termos literais de busca, staleness e orçamento de serialização das entradas."""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from lastro.knowledge.models import (
    MAX_QUERY_CHARACTERS,
    MAX_QUERY_TOKENS,
    STALE_AFTER_DAYS,
    JsonObject,
)
from lastro.knowledge.safety import escape_untrusted

_TOKEN = re.compile(r"\w+", re.UNICODE)

NEVER_VERIFIED = "never_verified"
OLDER_THAN_LIMIT = "older_than_30_days"
VERSION_CHANGED = "version_changed"


def literal_terms(query: str) -> list[str]:
    """Tokens como termos literais; operadores FTS do usuário nunca chegam ao MATCH."""
    return _TOKEN.findall(query)[:MAX_QUERY_TOKENS]


def match_expression(terms: list[str]) -> str | None:
    if not terms:
        return None
    # Cada termo vira string FTS entre aspas; aspas internas são duplicadas pela sintaxe FTS5.
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)


@dataclass(frozen=True)
class Staleness:
    stale: bool
    reasons: tuple[str, ...]
    version_unknown: bool


def staleness(
    last_verified_at: datetime | None,
    now: datetime,
    entry_version: str | None,
    current_version: str | None,
) -> Staleness:
    """Exatamente 30 dias ainda não é stale; versão ausente nunca é tratada como igual."""
    reasons: list[str] = []
    if last_verified_at is None:
        reasons.append(NEVER_VERIFIED)
    elif now - last_verified_at > timedelta(days=STALE_AFTER_DAYS):
        reasons.append(OLDER_THAN_LIMIT)
    unknown = entry_version is None or current_version is None
    if not unknown and entry_version != current_version:
        reasons.append(VERSION_CHANGED)
    return Staleness(stale=bool(reasons), reasons=tuple(reasons), version_unknown=unknown)


def encode(data: object) -> str:
    return escape_untrusted(json.dumps(data, ensure_ascii=False, separators=(",", ":")))


@dataclass(frozen=True)
class Budgeted:
    content: str
    included: tuple[JsonObject, ...]
    omitted: int


def fit_entries(
    entries: list[JsonObject], limit: int, budget: int = MAX_QUERY_CHARACTERS
) -> Budgeted:
    """Inclui entradas inteiras em ordem enquanto o JSON completo couber no orçamento.

    A contagem usa o conteúdo realmente serializado, com flags, metadados e marcadores; nada é
    truncado no meio de uma entrada.
    """
    included: list[JsonObject] = []
    omitted = 0
    for entry in entries:
        if len(included) >= limit:
            omitted += 1
            continue
        candidate = [*included, entry]
        if len(encode({"entries": candidate})) <= budget:
            included = candidate
        else:
            omitted += 1
    return Budgeted(
        content=encode({"entries": included}), included=tuple(included), omitted=omitted
    )
