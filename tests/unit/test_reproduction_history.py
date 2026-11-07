import sqlite3
from typing import Any

import pytest

from lastro.config.loader import ProjectContext
from lastro.history import (
    ArtifactType,
    AttemptDetail,
    AttemptReason,
    EventType,
    EvidencePurpose,
    EvidenceReference,
    ExpectedSource,
    FailureDomain,
    FindingClassification,
    FindingDraft,
    HistoryStore,
    InvalidEventError,
    ReportingError,
    ReproducerResult,
    ReproductionError,
    ReproductionOutcome,
    RoundStatus,
    RunStatus,
    TextMission,
    VerdictInput,
    VerdictReason,
    open_history,
)


def draft(**overrides: Any) -> FindingDraft:
    data: dict[str, Any] = {
        "title": "CPF duplicado aceito",
        "flow": "Cadastro de funcionário",
        "preconditions": ("Funcionário com CPF 123 existe",),
        "steps": ("Abrir cadastro", "Informar CPF 123", "Salvar"),
        "expected": "O cadastro é recusado",
        "expected_source": ExpectedSource.HEURISTIC,
        "expected_source_ref": None,
        "observed": "O cadastro foi salvo",
        "evidence": (),
        "classification": FindingClassification.SUSPECTED,
        "limitations": (),
        "failure_domain": FailureDomain.SYSTEM,
    }
    data.update(overrides)
    return FindingDraft(**data)


def shot(store: HistoryStore, run_id: str, name: str) -> str:
    return store.register_artifact(
        run_id, f"evidence/{name}.png", ArtifactType.SCREENSHOT, content=name.encode()
    ).id


def explored(context: ProjectContext) -> tuple[HistoryStore, str, str]:
    store = open_history(context)
    run_id = store.create_run(context, TextMission("Explore")).record.id
    evidence = shot(store, run_id, "explorer")
    finding = store.report_finding(
        run_id, draft(evidence=(EvidenceReference(evidence, EvidencePurpose.STATE),))
    ).finding
    store.finish_run(run_id, RunStatus.COMPLETED)
    return store, run_id, finding.id


def child(
    store: HistoryStore, context: ProjectContext, round_id: str, finding_id: str, ordinal: int
) -> str:
    return store.create_run(
        context, TextMission("Brief"), reproduction_of=(round_id, finding_id, ordinal)
    ).record.id


def reproduced(store: HistoryStore, context: ProjectContext, round_id: str, finding_id: str) -> str:
    child_id = child(store, context, round_id, finding_id, 1)
    evidence = shot(store, child_id, "repro")
    store.record_reproducer_report(
        child_id, ReproducerResult.REPRODUCED, (evidence,), None, "Duplicado aceito de novo."
    )
    store.finish_run(child_id, RunStatus.COMPLETED)
    store.record_attempt_detail(
        AttemptDetail(
            finding_id=finding_id,
            child_run_id=child_id,
            ordinal=1,
            outcome=ReproductionOutcome.REPRODUCED,
            reason=None,
            evidence_artifact_ids=(evidence,),
            note="",
            precondition_index=None,
            executor="claude-code",
        )
    )
    return child_id


def verdict(finding_id: str, round_id: str, **overrides: Any) -> VerdictInput:
    data: dict[str, Any] = {
        "finding_id": finding_id,
        "round_id": round_id,
        "attempts_planned": 2,
        "attempts_decisive": 1,
        "reproduced_count": 1,
        "classification": FindingClassification.CONFIRMED,
        "reason": None,
        "missing_precondition": None,
        "intermittent": False,
        "cross_executor": False,
        "explorer_executor": "claude-code",
        "verifier_executor": "claude-code",
        "notes": (),
    }
    data.update(overrides)
    return VerdictInput(**data)


def opened(store: HistoryStore, run_id: str, finding_id: str) -> str:
    return store.start_reproduction_round(
        run_id,
        mode="same",
        explorer="claude-code",
        verifier="claude-code",
        finding_ids=[finding_id],
    ).id


def test_verdict_is_immutable(history_context: ProjectContext) -> None:
    store, run_id, finding_id = explored(history_context)
    round_id = opened(store, run_id, finding_id)
    reproduced(store, history_context, round_id, finding_id)
    settled = store.settle_verdict(verdict(finding_id, round_id))

    assert settled.label == "confirmed (1/1)"
    assert store.get_finding(finding_id).classification == FindingClassification.CONFIRMED
    with sqlite3.connect(store.database_path) as conn:
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute("UPDATE finding_reproduction_verdicts SET reproduced_count = 0")
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute("DELETE FROM finding_reproduction_verdicts")


def test_verdict_is_unique_per_finding(history_context: ProjectContext) -> None:
    store, run_id, finding_id = explored(history_context)
    round_id = opened(store, run_id, finding_id)
    reproduced(store, history_context, round_id, finding_id)
    store.settle_verdict(verdict(finding_id, round_id))
    with pytest.raises(ReproductionError) as info:
        store.settle_verdict(verdict(finding_id, round_id))
    assert info.value.code == "reproduction_not_eligible"


def test_settle_inconclusive_rejects_confirmed_finding(history_context: ProjectContext) -> None:
    store, run_id, finding_id = explored(history_context)
    round_id = opened(store, run_id, finding_id)
    reproduced(store, history_context, round_id, finding_id)
    store.promote_finding(finding_id)
    inconclusive = verdict(
        finding_id,
        round_id,
        classification=FindingClassification.INCONCLUSIVE,
        reason=VerdictReason.NOT_REPRODUCED,
        reproduced_count=0,
    )
    with pytest.raises(ReproductionError):
        store.settle_verdict(inconclusive)
    from lastro.history import reporting

    with sqlite3.connect(store.database_path) as conn:
        conn.row_factory = sqlite3.Row
        with pytest.raises(ReportingError):
            reporting.settle_inconclusive(conn, finding_id, "2026-11-07T00:00:00Z")


def test_confirmed_needs_reproduced_child_attempt(history_context: ProjectContext) -> None:
    store, run_id, finding_id = explored(history_context)
    round_id = opened(store, run_id, finding_id)
    with pytest.raises(ReproductionError, match="child-run evidence"):
        store.settle_verdict(verdict(finding_id, round_id))
    assert store.get_finding(finding_id).classification == FindingClassification.SUSPECTED


def test_promote_still_requires_explorer_evidence(history_context: ProjectContext) -> None:
    store = open_history(history_context)
    run_id = store.create_run(history_context, TextMission("Explore")).record.id
    finding_id = store.report_finding(run_id, draft()).finding.id
    store.finish_run(run_id, RunStatus.COMPLETED)
    round_id = opened(store, run_id, finding_id)
    reproduced(store, history_context, round_id, finding_id)
    with pytest.raises(ReportingError):
        store.settle_verdict(verdict(finding_id, round_id))
    assert store.get_reproduction_verdict(finding_id) is None


def test_inconclusive_verdict_settles_finding(history_context: ProjectContext) -> None:
    store, run_id, finding_id = explored(history_context)
    round_id = opened(store, run_id, finding_id)
    settled = store.settle_verdict(
        verdict(
            finding_id,
            round_id,
            classification=FindingClassification.INCONCLUSIVE,
            reason=VerdictReason.BLOCKED,
            attempts_decisive=0,
            reproduced_count=0,
            notes=("attempt 1: blocked",),
        )
    )
    assert settled.label == "inconclusive (blocked)"
    assert store.get_finding(finding_id).classification == FindingClassification.INCONCLUSIVE
    events = [event.event_type for event in store.list_events(run_id)]
    assert events[-2:] == [EventType.REPRODUCTION_STARTED, EventType.REPRODUCTION_VERDICT]


def test_post_terminal_events_only_of_reproduction_types(history_context: ProjectContext) -> None:
    store, run_id, finding_id = explored(history_context)
    with pytest.raises(Exception, match=run_id):
        store.append_event(run_id, EventType.TOOL_CALL, tool_name="observe")
    from lastro.history import reproduction

    with sqlite3.connect(store.database_path) as conn:
        conn.row_factory = sqlite3.Row
        with pytest.raises(InvalidEventError):
            reproduction.append_reproduction_event(
                conn, run_id, EventType.TOOL_CALL, {}, "2026-11-07T00:00:00Z"
            )
    opened(store, run_id, finding_id)
    assert store.list_events(run_id)[-1].event_type == EventType.REPRODUCTION_STARTED


def test_single_open_round_per_run(history_context: ProjectContext) -> None:
    store, run_id, finding_id = explored(history_context)
    first = opened(store, run_id, finding_id)
    other = open_history(history_context, owner_pid=store.owner_pid + 1)
    with pytest.raises(ReproductionError) as info:
        other.start_reproduction_round(
            run_id, mode="same", explorer="claude-code", verifier="claude-code", finding_ids=[]
        )
    assert info.value.code == "reproduction_round_active"
    store.close_reproduction_round(first)
    with (
        sqlite3.connect(store.database_path) as conn,
        pytest.raises(sqlite3.DatabaseError, match="immutable"),
    ):
        conn.execute("UPDATE reproduction_rounds SET status = 'open'")


def test_dead_coordinator_round_is_taken_over(history_context: ProjectContext) -> None:
    store, run_id, finding_id = explored(history_context)
    first = opened(store, run_id, finding_id)
    other = open_history(
        history_context, owner_pid=store.owner_pid + 1, process_checker=lambda pid: False
    )
    resumed = other.start_reproduction_round(
        run_id, mode="same", explorer="claude-code", verifier="claude-code", finding_ids=[]
    )
    assert resumed.id == first and resumed.resumed


def test_round_needs_terminal_run(history_context: ProjectContext) -> None:
    store = open_history(history_context)
    run_id = store.create_run(history_context, TextMission("Explore")).record.id
    with pytest.raises(ReproductionError, match="still running"):
        opened(store, run_id, "none")


def test_reporter_contract(history_context: ProjectContext) -> None:
    store, run_id, finding_id = explored(history_context)
    round_id = opened(store, run_id, finding_id)
    child_id = child(store, history_context, round_id, finding_id, 1)
    foreign = shot(store, run_id, "foreign")

    def code(*args: Any) -> str:
        with pytest.raises(ReportingError) as info:
            store.record_reproducer_report(child_id, *args)
        return info.value.code

    assert code(ReproducerResult.REPRODUCED, (foreign,), None, "") == "invalid_evidence"
    assert code(ReproducerResult.REPRODUCED, (), None, "") == "invalid_evidence"
    assert code(ReproducerResult.PRECONDITION_MISSING, (), 3, "") == "invalid_precondition_index"
    assert code(ReproducerResult.NOT_REPRODUCED, (), 0, "") == "invalid_precondition_index"
    with pytest.raises(ReportingError) as info:
        store.record_reproducer_report(run_id, ReproducerResult.NOT_REPRODUCED, (), None, "")
    assert info.value.code == "not_a_reproduction_run"

    report = store.record_reproducer_report(
        child_id, ReproducerResult.PRECONDITION_MISSING, (), 0, "token=abc123secretvalue"
    )
    assert report.precondition_index == 0
    assert code(ReproducerResult.NOT_REPRODUCED, (), None, "") == "already_reported"


def test_attempt_detail_records_event_once(history_context: ProjectContext) -> None:
    store, run_id, finding_id = explored(history_context)
    round_id = opened(store, run_id, finding_id)
    child_id = child(store, history_context, round_id, finding_id, 1)
    store.finish_run(child_id, RunStatus.LIMIT_REACHED, "action_limit")
    detail = AttemptDetail(
        finding_id=finding_id,
        child_run_id=child_id,
        ordinal=1,
        outcome=ReproductionOutcome.INCONCLUSIVE,
        reason=AttemptReason.BUDGET,
        evidence_artifact_ids=(),
        note="",
        precondition_index=None,
        executor="claude-code",
    )
    first = store.record_attempt_detail(detail)
    again = store.record_attempt_detail(detail)
    assert first == again and first.reason == AttemptReason.BUDGET
    finished = [
        event
        for event in store.list_events(run_id)
        if event.event_type == EventType.REPRODUCTION_ATTEMPT_FINISHED
    ]
    assert len(finished) == 1
    assert store.reproduction_children(finding_id)[0].child_run_id == child_id
    with (
        sqlite3.connect(store.database_path) as conn,
        pytest.raises(sqlite3.DatabaseError, match="immutable"),
    ):
        conn.execute("UPDATE reproduction_runs SET ordinal = 2")


def test_child_run_is_not_a_resume(history_context: ProjectContext) -> None:
    store, run_id, finding_id = explored(history_context)
    round_id = opened(store, run_id, finding_id)
    child_id = child(store, history_context, round_id, finding_id, 1)
    assert store.parent_run(child_id) is None
    assert [event.event_type for event in store.list_events(child_id)] == [EventType.RUN_STARTED]
    status = store.reproduction_status(run_id)
    assert status.open_round is not None and status.eligible == 1
    store.close_reproduction_round(round_id, RoundStatus.ABANDONED)
    assert store.reproduction_status(run_id).open_round is None
