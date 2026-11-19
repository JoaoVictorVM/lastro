"""Fusão híbrida e orçamento da resposta: RRF reproduzível e só passagens inteiras citáveis.

Nenhum score vira confiança calibrada: a posição na lista é o único sinal entregue.
"""

import importlib
import json
import re
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from lastro.knowledge.models import JsonObject
from lastro.sources.models import (
    RESPONSE_CHARACTERS,
    RESPONSE_TOKENS,
    RRF_K,
    SOURCES_UNAVAILABLE,
    Candidate,
    Passage,
    SourcesError,
)

_TERM = re.compile(r"\w+", re.UNICODE)
MAX_TERMS = 200


def match_expression(query: str) -> str | None:
    """Termos literais em OR; operadores FTS do agente nunca chegam ao MATCH."""
    terms = _TERM.findall(query)[:MAX_TERMS]
    if not terms:
        return None
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)


def fuse(
    lexical: Sequence[Candidate], vector: Sequence[Candidate], limit: int
) -> list[tuple[Candidate, float]]:
    """Soma de 1/(60 + posição), posições a partir de 1 e pesos iguais.

    Desempate por caminho, ordinal e ID do chunk, para que a mesma consulta sobre a mesma
    geração produza sempre a mesma ordem.
    """
    scores: dict[str, float] = {}
    known: dict[str, Candidate] = {}
    for ranked in (lexical, vector):
        seen: set[str] = set()
        position = 0
        for candidate in ranked:
            if candidate.chunk_id in seen:
                continue
            seen.add(candidate.chunk_id)
            position += 1
            known[candidate.chunk_id] = candidate
            scores[candidate.chunk_id] = scores.get(candidate.chunk_id, 0.0) + 1.0 / (
                RRF_K + position
            )
    ordered = sorted(
        known.values(),
        key=lambda item: (-scores[item.chunk_id], item.relative_path, item.ordinal, item.chunk_id),
    )
    return [(item, scores[item.chunk_id]) for item in ordered[:limit]]


def response_size(passages: Sequence[Passage], omitted: int) -> int:
    body: JsonObject = {
        "query_id": "sq_" + "0" * 32,
        "generation": 0,
        "mode": "hybrid",
        "passages": [passage.as_json() for passage in passages],
        "truncated": omitted > 0,
        "omitted_count": omitted,
    }
    return len(json.dumps(body, ensure_ascii=False, separators=(",", ":")))


def fit_passages(passages: Sequence[Passage]) -> tuple[list[Passage], int]:
    """Mantém a ordem e descarta inteiros os de menor ranking até caber nos dois limites."""
    included = list(passages)
    while included and (
        sum(item.token_count for item in included) > RESPONSE_TOKENS
        or response_size(included, len(passages) - len(included)) > RESPONSE_CHARACTERS
    ):
        included.pop()
    return included, len(passages) - len(included)


def trusted_extension() -> str:
    """Biblioteca do pacote sqlite-vec instalado; nenhum caminho vem de configuração ou agente."""
    try:
        sqlite_vec: Any = importlib.import_module("sqlite_vec")
    except ImportError:
        raise SourcesError(
            SOURCES_UNAVAILABLE, "sqlite-vec não instalado. Rode uv sync --extra sources."
        ) from None
    package = Path(str(sqlite_vec.__file__)).resolve().parent
    library = Path(str(sqlite_vec.loadable_path())).resolve()
    candidates = [library, *library.parent.glob(library.name + ".*")]
    found = next((item for item in candidates if item.is_file()), None)
    if found is None or not found.is_relative_to(package):
        raise SourcesError(
            SOURCES_UNAVAILABLE, "Extensão sqlite-vec fora do pacote instalado; carga recusada."
        )
    return str(library)


def vec_connector(*args: Any, **kwargs: Any) -> sqlite3.Connection:
    """Conexão de recuperação: carrega só a extensão confiável e desliga a carga em seguida."""
    path = trusted_extension()
    conn = sqlite3.connect(*args, **kwargs)
    try:
        conn.enable_load_extension(True)
        try:
            conn.load_extension(path)
        finally:
            conn.enable_load_extension(False)
    except (AttributeError, sqlite3.Error):
        conn.close()
        raise SourcesError(
            SOURCES_UNAVAILABLE, "Este Python não permite carregar a extensão sqlite-vec."
        ) from None
    return conn
