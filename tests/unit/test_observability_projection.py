import subprocess
import sys
from typing import Any

import pytest

from lastro.history.models import EventRecord, EventType, PolicyDecision
from lastro.observability.projection import (
    STAGES,
    ProjectionError,
    RunProjection,
    describe,
    describe_all,
    span_id_for,
    stage_of,
    trace_id_for,
)

PROJECT = "3f0c9a52-6b1e-4d8e-9a77-2a4f1c0d5e61"
RUN = "run_20261107_120000_a1b2"
START = "2026-11-07T12:00:00.000000Z"
END = "2026-11-07T12:05:00.000000Z"


def event(
    event_id: int,
    event_type: EventType,
    *,
    tool: str | None = None,
    decision: PolicyDecision | None = None,
    correlation: str | None = None,
    payload: Any = None,
    timestamp: str = "2026-11-07T12:01:00.000000Z",
) -> EventRecord:
    return EventRecord(
        id=event_id,
        run_id=RUN,
        sequence=event_id,
        timestamp=timestamp,
        event_type=event_type,
        tool_name=tool,
        arguments_sha256=None,
        arguments_preview=None,
        policy_decision=decision,
        correlation_id=correlation,
        idempotency_key=None,
        payload=payload,
        result=None,
    )


def projection(**overrides: Any) -> RunProjection:
    values: dict[str, Any] = {
        "project_id": PROJECT,
        "run_id": RUN,
        "executor": "codex",
        "started_at": START,
        "ended_at": END,
        "lastro_version": "0.1.0",
        "root_event_id": 1,
    }
    values.update(overrides)
    return RunProjection(**values)


def test_ids_are_stable_across_processes() -> None:
    code = (
        "from lastro.observability.projection import trace_id_for, span_id_for;"
        f"print(trace_id_for('{PROJECT}', '{RUN}'), span_id_for('{PROJECT}', '{RUN}', 7))"
    )
    other = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.split()

    assert other == [trace_id_for(PROJECT, RUN), span_id_for(PROJECT, RUN, 7)]
    assert len(other[0]) == 32 and len(other[1]) == 16
    assert other[0] == other[0].lower() and int(other[0], 16) != 0
    assert trace_id_for(PROJECT, RUN) != trace_id_for(PROJECT, "run_20261107_120000_ffff")
    assert span_id_for(PROJECT, RUN, 7) != span_id_for(PROJECT, RUN, 8)


def test_root_maps_to_run_started() -> None:
    run = projection()
    root = describe(event(1, EventType.RUN_STARTED), run)
    child = describe(event(2, EventType.TOOL_CALL, tool="observe"), run)

    assert root.parent_span_id is None
    assert root.span_id == run.root_span_id
    assert (root.start_time, root.end_time) == (START, END)
    assert root.attributes["lastro.duration.ms"] == 300_000
    assert root.attributes["lastro.stage"] == "mission_preparation"
    assert child.parent_span_id == root.span_id
    assert child.trace_id == root.trace_id == trace_id_for(PROJECT, RUN)
    with pytest.raises(ProjectionError):
        describe(event(1, EventType.RUN_STARTED), projection(ended_at=None))


def test_every_event_has_one_descriptor() -> None:
    events = [event(index + 1, kind) for index, kind in enumerate(EventType)]
    events = [
        item
        if item.event_type != EventType.AUTHENTICATION
        else event(item.id, item.event_type, payload={"subtype": "login"})
        for item in events
    ]
    descriptors = describe_all(events, projection())

    assert len(descriptors) == len(events)
    assert len({item.span_id for item in descriptors}) == len(events)
    assert [item.attributes["lastro.event.id"] for item in descriptors] == [
        item.id for item in events
    ]


def test_stage_mapping_precedence() -> None:
    cases = {
        ("tool_call", "knowledge_query"): "memory_lookup",
        ("tool_call", "observe"): "observation",
        ("tool_call", "submit"): "action",
        ("tool_call", "mission_context"): "mission_preparation",
        ("tool_call", "approval_status"): "approval",
        ("tool_call", "report_finding"): "verification",
        ("policy_decision", "observe"): "policy_evaluation",
        ("approval_requested", None): "approval",
        ("oracle_signal", None): "verification",
        ("reproduction_verdict", None): "reproduction",
        ("model_call_completed", None): "model_call",
        ("limit_hit", None): "lifecycle",
    }
    for (kind, tool), expected in cases.items():
        stage, _ = stage_of(event(5, EventType(kind), tool=tool))
        assert stage == expected, (kind, tool)
    consolidation = event(6, EventType.KNOWLEDGE_PROPOSED, payload={"subtype": "consolidation"})
    assert stage_of(consolidation)[0] == "consolidation"
    assert set(STAGES) >= {"memory_lookup", "consolidation", "reproduction"}


def test_denied_action_is_not_executed() -> None:
    run = projection(decisions={"act_1": PolicyDecision.DENY, "act_2": PolicyDecision.ALLOW})
    denied = describe(event(3, EventType.TOOL_CALL, tool="submit", correlation="act_1"), run)
    allowed = describe(event(4, EventType.TOOL_CALL, tool="observe", correlation="act_2"), run)
    gated = describe(
        event(
            5,
            EventType.POLICY_DECISION,
            tool="submit",
            decision=PolicyDecision.REQUIRE_APPROVAL,
            correlation="act_3",
        ),
        run,
    )

    assert denied.attributes["lastro.operation.status"] == "not_executed"
    assert denied.attributes["lastro.policy.decision"] == "deny"
    assert allowed.attributes["lastro.operation.status"] == "attempted"
    assert gated.attributes["lastro.operation.status"] == "not_executed"
    assert gated.attributes["langfuse.trace.tags"] == [
        "codex",
        "policy_evaluation",
        "require_approval",
    ]


def test_instant_event_has_no_invented_duration() -> None:
    descriptor = describe(event(3, EventType.ORACLE_SIGNAL), projection())

    assert descriptor.start_time == descriptor.end_time
    assert descriptor.attributes["lastro.duration.status"] == "unavailable"
    assert "lastro.duration.ms" not in descriptor.attributes

    run = projection(approval_requests={"act_9": "2026-11-07T12:00:30.000000Z"})
    decided = describe(
        event(4, EventType.APPROVAL_DECIDED, payload={"action_id": "act_9", "status": "approved"}),
        run,
    )
    assert decided.attributes["lastro.duration.status"] == "derived"
    assert decided.attributes["lastro.duration.ms"] == 30_000
    skewed = projection(approval_requests={"act_9": "2026-11-07T12:02:00.000000Z"})
    late = describe(event(4, EventType.APPROVAL_DECIDED, payload={"action_id": "act_9"}), skewed)
    assert late.attributes["lastro.duration.ms"] == 0
    assert late.attributes["lastro.duration.diagnostic"] == "clock_skew"


def test_consolidation_nothing_new_uses_run_ended() -> None:
    ended = event(9, EventType.RUN_ENDED, payload={"status": "completed", "reason": None})
    descriptor = describe(ended, projection(memory_outcome="nothing_new"))

    assert descriptor.attributes["lastro.stage"] == "consolidation"
    assert descriptor.attributes["lastro.stage.categories"] == ["lifecycle"]
    assert (
        describe(ended, projection(memory_outcome="updated")).attributes["lastro.stage"]
        == "lifecycle"
    )


def test_unknown_tool_is_replaced() -> None:
    descriptor = describe(event(3, EventType.TOOL_CALL, tool="exfiltrate_cookies"), projection())

    assert descriptor.attributes["lastro.tool.name"] == "unknown"
    assert "exfiltrate" not in descriptor.name
    assert descriptor.name == "action.tool_call.unknown"
