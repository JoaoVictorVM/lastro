"""Proveniência de versão: código analisado contra versão realmente executada no alvo."""

import json
import re

from lastro.change_context.models import VersionMatch

_FULL_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_VERSION_KEYS = ("commit", "sha", "git_sha", "revision", "version")
MAX_VERSION_LENGTH = 200


def is_full_oid(value: str | None) -> bool:
    return value is not None and _FULL_OID.match(value) is not None


def version_match(analyzed: str | None, executed: str | None, dirty: bool) -> VersionMatch:
    """Só OIDs completos e iguais, sem mudanças locais, provam a correspondência.

    Tags, nomes de branch e versões opacas permanecem `unknown`; com working tree sujo, o mesmo
    HEAD não comprova os buffers locais analisados.
    """
    if not is_full_oid(analyzed) or not is_full_oid(executed):
        return VersionMatch.UNKNOWN
    if analyzed != executed:
        return VersionMatch.MISMATCHED
    return VersionMatch.DIRTY_UNVERIFIED if dirty else VersionMatch.MATCHED


def parse_version_response(body: str) -> str | None:
    """Versão do endpoint do perfil: JSON com chave conhecida ou texto curto em uma linha."""
    text = body.strip()
    if not text:
        return None
    try:
        data: object = json.loads(text)
    except ValueError:
        data = text
    if isinstance(data, dict):
        for key in _VERSION_KEYS:
            value = data.get(key)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if isinstance(value, str) and value.strip():
                return _clean(value)
        return None
    if isinstance(data, str) and "\n" not in data:
        return _clean(data)
    return None


def _clean(value: str) -> str | None:
    value = value.strip()
    if not value or len(value) > MAX_VERSION_LENGTH or any(ch.isspace() for ch in value):
        return None
    return value.lower() if is_full_oid(value.lower()) else value
