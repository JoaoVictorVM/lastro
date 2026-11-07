"""Auditoria anterior ao efeito de cada chamada."""

from typing import cast

from lastro.browser.policy import Decision
from lastro.history import EventType, HistoryStore, JsonObject, JsonValue


def decision_signature(tool: str, decision: Decision) -> JsonObject | None:
    """Assinatura canônica (tool, operação, entidade), comparável entre runs diferentes."""
    components = decision.components
    if components is None:
        return None
    target = components.get("target")
    fields = cast(dict[str, JsonValue], target) if isinstance(target, dict) else {}
    return {
        "tool": str(fields.get("tool") or tool),
        "operation": str(components.get("operation") or ""),
        "entity": str(fields.get("entity") or ""),
    }


def record_call(
    history: HistoryStore,
    run_id: str,
    action_id: str,
    tool: str,
    arguments: object,
    decision: Decision,
    details: JsonObject | None = None,
) -> int:
    event = history.append_event(
        run_id, EventType.TOOL_CALL, tool_name=tool, arguments=arguments, correlation_id=action_id
    )
    history.append_event(
        run_id,
        EventType.POLICY_DECISION,
        tool_name=tool,
        correlation_id=action_id,
        policy_decision=decision.effect,
        payload={
            "reason": decision.reason,
            "fingerprint": decision.fingerprint,
            **(
                {"signature": signed}
                if (signed := decision_signature(tool, decision)) is not None
                else {}
            ),
            **(details or {}),
        },
    )
    return event.sequence
