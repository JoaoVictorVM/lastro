"""Classifica o que a CLI emite como protocolo; texto do modelo nunca decide um término."""

import json
from dataclasses import dataclass
from typing import Any, cast

from lastro.history.redaction import redact_text

MAX_LINE_BYTES = 1024 * 1024
MAX_DISPLAY_CHARS = 500
REASON_LIMIT = "executor_limit"
REASON_RATE_LIMIT = "executor_rate_limit"
REASON_NETWORK = "executor_network"
REASON_ERROR = "executor_error"
REASON_PROTOCOL = "executor_protocol_error"
REASON_EXITED = "executor_exited"
REASON_MCP = "mcp_unavailable"
# Enumerações presentes no binário 0.166.0 da CLI.
LIMIT_CODES = frozenset({"usage_limit_exceeded"})
LIMIT_REACHED_TYPES = frozenset(
    {
        "usage_limit_reached",
        "workspace_owner_usage_limit_reached",
        "workspace_member_usage_limit_reached",
        "workspace_owner_credits_depleted",
        "workspace_member_credits_depleted",
    }
)
RATE_LIMIT_CODES = frozenset({"rate_limit_exceeded", "server_overloaded"})
NETWORK_CODES = frozenset(
    {
        "http_connection_failed",
        "response_stream_connection_failed",
        "response_stream_disconnected",
    }
)
# Texto fixo da CLI, emitido em evento de protocolo quando não há código estruturado.
LIMIT_MESSAGE_PREFIXES = ("You\u2019ve hit your usage limit", "You've hit your usage limit")
MCP_STARTUP_MARKER = "required MCP servers failed to initialize"


class CodexProtocolError(Exception):
    """Linha JSONL inválida, truncada ou maior que o limite."""


@dataclass(frozen=True)
class Failure:
    reason: str


def parse_line(line: bytes) -> dict[str, Any]:
    if len(line) > MAX_LINE_BYTES:
        raise CodexProtocolError("line exceeds the size limit")
    try:
        data: object = json.loads(line)
    except ValueError:
        raise CodexProtocolError("invalid JSONL line") from None
    if not isinstance(data, dict):
        raise CodexProtocolError("JSONL line is not an object")
    return cast(dict[str, Any], data)


def _error_code(error: dict[str, Any]) -> str | None:
    info = error.get("codex_error_info")
    if isinstance(info, str):
        return info
    if isinstance(info, dict):
        keys = list(cast(dict[str, Any], info))
        return keys[0] if len(keys) == 1 else None
    return None


def _classify_error(error: dict[str, Any]) -> str | None:
    code = _error_code(error)
    if code in LIMIT_CODES:
        return REASON_LIMIT
    if code in RATE_LIMIT_CODES:
        return REASON_RATE_LIMIT
    if code in NETWORK_CODES:
        return REASON_NETWORK
    message = error.get("message")
    if isinstance(message, str) and message.startswith(LIMIT_MESSAGE_PREFIXES):
        return REASON_LIMIT
    return None


def classify_exec_event(event: dict[str, Any]) -> Failure | None:
    """turn.failed encerra; `error` avulso também avisa reconexão, então só limite encerra."""
    kind = event.get("type")
    if kind == "turn.failed":
        raw = event.get("error")
        error = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
        return Failure(_classify_error(error) or REASON_ERROR)
    if kind == "error" and _classify_error(event) == REASON_LIMIT:
        return Failure(REASON_LIMIT)
    return None


def classify_rollout_record(record: dict[str, Any]) -> Failure | None:
    """Registro da sessão interativa: somente eventos da CLI, nunca itens de resposta."""
    if record.get("type") != "event_msg":
        return None
    raw = record.get("payload")
    payload = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
    kind = payload.get("type")
    if kind == "error" and _classify_error(payload) == REASON_LIMIT:
        return Failure(REASON_LIMIT)
    if kind == "token_count":
        limits = payload.get("rate_limits")
        if isinstance(limits, dict):
            reached = cast(dict[str, Any], limits).get("rate_limit_reached_type")
            if reached in LIMIT_REACHED_TYPES:
                return Failure(REASON_LIMIT)
    return None


def display_text(event: dict[str, Any]) -> str | None:
    """Progresso exibido no headless: só a mensagem do agente, redigida e limitada."""
    if event.get("type") != "item.completed":
        return None
    raw = event.get("item")
    item = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
    text = item.get("text")
    if item.get("type") != "agent_message" or not isinstance(text, str):
        return None
    safe = redact_text(text)
    return safe[:MAX_DISPLAY_CHARS] + ("..." if len(safe) > MAX_DISPLAY_CHARS else "")
