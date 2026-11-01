"""Auditoria anterior ao efeito de cada chamada."""

from lastro.browser.policy import Decision
from lastro.history import EventType, HistoryStore


def record_call(
    history: HistoryStore,
    run_id: str,
    action_id: str,
    tool: str,
    arguments: object,
    decision: Decision,
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
        payload={"reason": decision.reason, "fingerprint": decision.fingerprint},
    )
    return event.sequence
