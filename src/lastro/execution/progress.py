"""Detector de progresso pelo event log e identidade confiável de operações.

As regras são puras: o histórico aplica o resultado na mesma transação da conclusão da ação.
"""

from collections.abc import Iterable, Mapping
from typing import Any, cast
from urllib.parse import urlsplit

# Eventos externos ao controlador cujo primeiro aparecimento conta como novidade.
DOMAIN_EVENT_TYPES = frozenset(
    {
        "authentication",
        "approval_decided",
        "clarification_requested",
        "clarification_answered",
        "oracle_signal",
        "record_created",
        "finding_submitted",
        "knowledge_proposed",
    }
)
# Auditoria de chamadas: só a primeira ocorrência do tipo conta, nunca um resultado textual novo.
FIRST_ONLY_TYPES = frozenset({"tool_call", "policy_decision"})
# Produzidos pelo próprio controle ou pelo ciclo de vida: nunca reiniciam o detector.
CONTROLLER_EVENT_TYPES = frozenset(
    {
        "run_started",
        "run_ended",
        "limit_hit",
        "execution_warning",
        "cancellation_requested",
        "write_reconciled",
        "run_resumed",
    }
)
# Campos de transporte que não mudam a intenção da operação.
TRANSPORT_ARGUMENTS = frozenset({"approval_id", "approval_proof", "reason", "timeout_ms"})
RECOVERABLE_TOOLS = frozenset({"navigate", "click", "fill", "select", "submit", "wait_for"})


def page_key(url: str) -> str | None:
    """Origem e caminho canônicos; query, fragmento e texto livre não criam novidade."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    path = parts.path.rstrip("/") or "/"
    return f"page:{parts.scheme}://{parts.hostname.lower()}:{parts.port or ''}{path}"


def event_keys(event_type: str, payload: object) -> list[str]:
    """Chaves de novidade de um evento; vazio para ruído, duplicata ou recusa."""
    data: Mapping[str, Any] = cast(Mapping[str, Any], payload) if isinstance(payload, dict) else {}
    if event_type in CONTROLLER_EVENT_TYPES:
        return []
    if event_type in FIRST_ONLY_TYPES:
        return [f"type:{event_type}"]
    if event_type not in DOMAIN_EVENT_TYPES:
        return []
    if event_type == "knowledge_proposed":
        # Só proposta aceita é novidade; recusas compartilham o tipo, mas não contam.
        if data.get("subtype") != "accepted":
            return []
        content = data.get("content_sha256")
        keys = ["type:knowledge_proposed"]
        if isinstance(content, str):
            keys.append(f"knowledge:{content}")
        return keys
    keys = [f"type:{event_type}"]
    if event_type == "finding_submitted":
        finding = data.get("finding_id")
        if isinstance(finding, str):
            keys.append(f"finding:{finding}")
    return keys


def new_keys(seen: set[str], events: Iterable[tuple[str, object]], page: str | None) -> list[str]:
    """Chaves inéditas na run, em ordem; o conjunto pertence à run inteira."""
    found: list[str] = []
    candidates = [key for event_type, payload in events for key in event_keys(event_type, payload)]
    if page is not None:
        candidates.append(page)
    for key in candidates:
        if key not in seen and key not in found:
            found.append(key)
    return found


def operation_key(tool: str, arguments: Mapping[str, Any], tenant: str, route: str) -> str | None:
    """Identidade calculada pelo servidor: o agente não escolhe nem informa o contador."""
    # Importação tardia: o histórico usa este módulo e não pode ser carregado por ele no topo.
    from lastro.history.redaction import canonical_json, redact, sha256_hex

    if tool not in RECOVERABLE_TOOLS:
        return None
    canonical = {
        key: value for key, value in sorted(arguments.items()) if key not in TRANSPORT_ARGUMENTS
    }
    return sha256_hex(
        canonical_json(
            redact({"tool": tool, "tenant": tenant, "route": route, "arguments": canonical})
        )
    )
