from typing import Any

from lastro.history import (
    AttemptReason,
    EventRecord,
    EventType,
    PolicyDecision,
    ReproducerReport,
    ReproducerResult,
    ReproductionOutcome,
    RunStatus,
)
from lastro.oracles.models import OracleOccurrence, OracleSignal
from lastro.reproduction import AttemptFacts, interpret
from lastro.reproduction.interpret import (
    authentication_failed,
    oracle_identity,
    policy_signatures,
)

PRECONDITIONS = ("Funcionário com CPF 123 existe", "Usuário com perfil gestor")


def report(result: ReproducerResult, *evidence: str, index: int | None = None) -> ReproducerReport:
    return ReproducerReport(
        child_run_id="run_child",
        result=result,
        evidence_artifact_ids=evidence,
        precondition_index=index,
        note="nota",
        created_at="2026-11-07T00:00:00Z",
    )


def facts(**overrides: Any) -> AttemptFacts:
    data: dict[str, Any] = {
        "ordinal": 1,
        "preconditions": PRECONDITIONS,
        "child_status": RunStatus.COMPLETED,
        "child_artifact_ids": frozenset({"art_child"}),
    }
    data.update(overrides)
    return AttemptFacts(**data)


def event(kind: EventType, payload: object, decision: PolicyDecision | None = None) -> EventRecord:
    return EventRecord(
        id=1,
        run_id="run",
        sequence=1,
        timestamp="2026-11-07T00:00:00Z",
        event_type=kind,
        tool_name="submit",
        arguments_sha256=None,
        arguments_preview=None,
        policy_decision=decision,
        correlation_id=None,
        idempotency_key=None,
        payload=payload,  # type: ignore[arg-type]
        result=None,
    )


def signal(path: str, status: int = 500) -> OracleSignal:
    occurrence = OracleOccurrence(
        occurrence_id="o1",
        signal_type="http_5xx",
        url=None,
        url_path=path,
        http_method="POST",
        http_status=status,
        message="boom",
        timestamp="2026-11-07T00:00:00.000000Z",
        action_id=None,
        action_sequence=None,
    )
    return OracleSignal(
        id="sig",
        run_id="run",
        signal_type="http_5xx",
        url_path=path,
        message="boom",
        first_seen_at="",
        last_seen_at="",
        occurrence_count=1,
        first_occurrence=occurrence,
        last_occurrence=occurrence,
        last_event_id=1,
    )


def test_reproduced_needs_child_run_evidence() -> None:
    result = interpret(facts(report=report(ReproducerResult.REPRODUCED, "art_child")))
    assert result.outcome == ReproductionOutcome.REPRODUCED
    assert result.evidence_artifact_ids == ("art_child",)


def test_foreign_artifact_rejected() -> None:
    result = interpret(facts(report=report(ReproducerResult.REPRODUCED, "art_parent")))
    assert result.outcome == ReproductionOutcome.INCONCLUSIVE
    assert result.reason == AttemptReason.EVIDENCE


def test_precondition_index_uses_brief_text() -> None:
    result = interpret(facts(report=report(ReproducerResult.PRECONDITION_MISSING, index=1)))
    assert result.reason == AttemptReason.PRECONDITIONS
    assert result.missing_precondition == "Usuário com perfil gestor"
    invalid = interpret(facts(report=report(ReproducerResult.PRECONDITION_MISSING, index=7)))
    assert invalid.reason == AttemptReason.EVIDENCE


def test_policy_deny_on_previously_allowed_signature_is_blocked() -> None:
    signed = {"signature": {"tool": "submit", "operation": "create", "entity": "employee"}}
    allowed = policy_signatures(
        [event(EventType.POLICY_DECISION, signed, PolicyDecision.ALLOW)], PolicyDecision.ALLOW
    )
    denied = policy_signatures(
        [event(EventType.POLICY_DECISION, signed, PolicyDecision.DENY)], PolicyDecision.DENY
    )
    result = interpret(
        facts(
            report=report(ReproducerResult.NOT_REPRODUCED),
            allowed_signatures=frozenset(allowed),
            denied_signatures=frozenset(denied),
        )
    )
    assert result.reason == AttemptReason.BLOCKED
    unrelated = interpret(
        facts(report=report(ReproducerResult.NOT_REPRODUCED), denied_signatures=frozenset(denied))
    )
    assert unrelated.outcome == ReproductionOutcome.NOT_REPRODUCED


def test_tenant_failure_is_blocked() -> None:
    events = [event(EventType.AUTHENTICATION, {"subtype": "tenant_mismatch"})]
    assert authentication_failed(events)
    result = interpret(
        facts(child_status=RunStatus.BLOCKED, child_reason="tenant_mismatch", auth_failed=True)
    )
    assert result.reason == AttemptReason.BLOCKED


def test_limit_reached_maps_to_budget() -> None:
    result = interpret(facts(child_status=RunStatus.LIMIT_REACHED, child_reason="action_limit"))
    assert result.reason == AttemptReason.BUDGET
    timed = interpret(facts(child_status=RunStatus.LIMIT_REACHED, child_reason="duration_limit"))
    assert timed.reason == AttemptReason.BUDGET


def test_oracle_finding_needs_same_oracle_signal() -> None:
    required = frozenset({oracle_identity(signal("/employees/12"))})
    claimed = report(ReproducerResult.REPRODUCED, "art_child")
    missing = interpret(facts(report=claimed, required_oracles=required))
    assert missing.reason == AttemptReason.EVIDENCE
    observed = interpret(
        facts(
            report=claimed,
            required_oracles=required,
            child_oracles=frozenset({oracle_identity(signal("/employees/40"))}),
        )
    )
    assert observed.outcome == ReproductionOutcome.REPRODUCED


def test_unavailable_executor_and_missing_report() -> None:
    assert (
        interpret(facts(executor_available=False, executor_note="install")).reason
        == AttemptReason.EXECUTOR
    )
    silent = interpret(facts(child_status=RunStatus.INTERRUPTED))
    assert silent.reason == AttemptReason.EVIDENCE
    diverged = interpret(facts(report=report(ReproducerResult.DIVERGED)))
    assert diverged.reason == AttemptReason.DIVERGED
