"""Redação de segredos antes de qualquer serialização: JSON canônico, preview e SHA-256.

Toda via de gravação do histórico (eventos, resultados, erros, fallback e snapshot de
configuração) passa por aqui, para que nenhum segredo influencie bytes persistidos.
"""

import hashlib
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from lastro.history.errors import InvalidEventError
from lastro.history.models import MAX_PREVIEW_LENGTH, JsonValue

REDACTED = "[REDACTED]"
PREVIEW_ELLIPSIS = "..."

SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "cookie",
    "apikey",
    "session",
    "credential",
)

_KEY_WORD = (
    r"(?<![\w-])[\w-]*"
    r"(?:password|passwd|secret|token|authorization|cookie|api[-_]?key|session|credential)"
    r"[\w-]*"
)

_BEARER = re.compile(r"(?i)\b(bearer)(\s+)(?!\[REDACTED\])[A-Za-z0-9\-._~+/=]{8,}")
# Linha de header: tudo depois dos dois pontos é segredo (cookies inteiros, por exemplo).
_HEADER_LINE = re.compile(
    r"(?im)^(\s*(?:proxy-)?(?:authorization|cookie|set-cookie|x-api-key|api-key|x-auth-token)"
    r"\s*:\s*)[^\r\n]+"
)
# Pares chave=valor ou "chave": "valor" dentro de texto livre, incluindo query strings.
_KEY_VALUE = re.compile(
    rf"(?i)({_KEY_WORD}[\"']?\s*[:=]\s*[\"']?)(?!\[REDACTED\])([^\"'\s&,;}}\]]+)"
)


def normalize_key(key: str) -> str:
    return key.lower().replace("-", "").replace("_", "")


def is_sensitive_key(key: str) -> bool:
    normalized = normalize_key(key)
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def redact_text(text: str) -> str:
    text = _HEADER_LINE.sub(lambda match: match.group(1) + REDACTED, text)
    text = _BEARER.sub(lambda match: match.group(1) + match.group(2) + REDACTED, text)
    return _KEY_VALUE.sub(lambda match: match.group(1) + REDACTED, text)


def redact(value: object) -> JsonValue:
    """Copia o valor redigindo chaves sensíveis e padrões de segredo em textos.

    Aceita apenas tipos JSON; qualquer outro tipo é rejeitado antes da gravação.
    """
    if value is None or isinstance(value, bool | int):
        return value  # pyright: ignore[reportReturnType]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidEventError("payload aceita apenas números finitos")
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        result: dict[str, JsonValue] = {}
        entries = cast(dict[object, object], value)
        for key, item in entries.items():
            if not isinstance(key, str):
                raise InvalidEventError("payload aceita apenas chaves de texto")
            result[key] = REDACTED if is_sensitive_key(key) else redact(item)
        return result
    if isinstance(value, list | tuple):
        return [redact(item) for item in cast(Sequence[object], value)]
    raise InvalidEventError(f"payload aceita apenas JSON (recebido {type(value).__name__})")


def canonical_json(value: JsonValue) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def sha256_hex(data: str | bytes) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def preview(text: str, limit: int = MAX_PREVIEW_LENGTH) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(PREVIEW_ELLIPSIS)] + PREVIEW_ELLIPSIS


@dataclass(frozen=True)
class RedactedJson:
    value: JsonValue
    canonical: str
    sha256: str
    preview: str


def redact_to_json(value: object) -> RedactedJson:
    """Hash e preview derivam do mesmo JSON canônico já redigido."""
    redacted = redact(value)
    canonical = canonical_json(redacted)
    return RedactedJson(
        value=redacted,
        canonical=canonical,
        sha256=sha256_hex(canonical),
        preview=preview(canonical),
    )
