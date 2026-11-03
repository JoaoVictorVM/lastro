import sqlite3
from pathlib import Path
from typing import Any

import pytest

from lastro.config.loader import ProjectContext
from lastro.history import (
    CONFIRMED_NEEDS_EVIDENCE,
    EVIDENCE_OUTSIDE_RUN,
    ArtifactType,
    CriterionOutcome,
    CriterionResultDraft,
    EventType,
    EvidencePurpose,
    EvidenceReference,
    ExpectedSource,
    FailureDomain,
    FindingClassification,
    FindingDraft,
    HistoryStore,
    ReportingError,
    ReproductionOutcome,
    ResultSource,
    RunNotActiveError,
    RunStatus,
    TextMission,
    open_history,
)
from lastro.history import reporting as sql
from lastro.history.database import Database
from lastro.history.migrations import MIGRATIONS, apply_migrations
from lastro.history.runs import insert_run
from lastro.mission import MissionPlanInput

CRITERION = {"text": "O telefone salvo permanece após recarregar", "source": "mission_explicit"}


def plan(*criteria: dict[str, str]) -> MissionPlanInput:
    return MissionPlanInput.model_validate(
        {
            "objectives": ["Validar o cadastro"],
            "scope": ["Editar funcionário"],
            "criteria": list(criteria) or [CRITERION],
            "gaps": [],
            "out_of_scope": [],
            "charter": {
                "target": "employee registration",
                "resources": ["demo application"],
                "information": "persistence defects",
                "heuristics": ["persistence_after_reload"],
            },
        }
    )


def draft(**overrides: Any) -> FindingDraft:
    data: dict[str, Any] = {
        "title": "Telefone não persiste após recarregar",
        "flow": "Cadastro de funcionário",
        "preconditions": ("Usuário autenticado no tenant esperado",),
        "steps": ("Abrir funcionário", "Preencher telefone", "Salvar", "Recarregar"),
        "expected": "O telefone salvo permanece visível",
        "expected_source": ExpectedSource.HEURISTIC,
        "expected_source_ref": None,
        "observed": "O campo aparece vazio após o reload.",
        "evidence": (),
        "classification": FindingClassification.SUSPECTED,
        "limitations": (),
        "failure_domain": FailureDomain.SYSTEM,
    }
    data.update(overrides)
    return FindingDraft(**data)


def count(store: HistoryStore, table: str) -> int:
    conn = sqlite3.connect(store.database_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def started(context: ProjectContext) -> tuple[HistoryStore, str]:
    store = open_history(context)
    return store, store.create_run(context, TextMission("Explore")).record.id


def screenshot(store: HistoryStore, run_id: str, name: str = "shot") -> str:
    return store.register_artifact(
        run_id, f"evidence/{name}.png", ArtifactType.SCREENSHOT, content=b"png-" + name.encode()
    ).id


def test_migrate_v004_database_to_v005(tmp_path: Path, history_context: ProjectContext) -> None:
    database = Database(tmp_path / "history.sqlite")
    apply_migrations(database, MIGRATIONS[:4])
    record = open_history(history_context).create_run(history_context, TextMission("m")).record
    database.write(lambda conn: insert_run(conn, record))
    database.write(
        lambda conn: conn.execute(
            "INSERT INTO findings (id, run_id, classification, created_at, updated_at) "
            "VALUES ('legacy', ?, 'suspected', 'x', 'x')",
            (record.id,),
        )
    )

    assert apply_migrations(database) == [5, 6]

    with database.connection() as conn:
        legacy = conn.execute("SELECT * FROM findings WHERE id = 'legacy'").fetchone()
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert (legacy["version"], legacy["failure_domain"], legacy["origin"]) == (1, None, None)
    assert {
        "finding_steps",
        "finding_evidence",
        "finding_reproduction_attempts",
        "criterion_results",
        "report_snapshots",
    } <= tables
    fresh = Database(tmp_path / "fresh.sqlite")
    assert apply_migrations(fresh) == [1, 2, 3, 4, 5, 6]


def test_finding_persists_fields_with_server_provenance(history_context: ProjectContext) -> None:
    store, run_id = started(history_context)
    shot = screenshot(store, run_id)

    submission = store.report_finding(
        run_id,
        draft(
            evidence=(EvidenceReference(shot, EvidencePurpose.STATE),),
            limitations=("Somente no Chromium",),
            action_id="action-1",
        ),
    )

    finding = submission.finding
    assert submission.created
    assert finding.id.startswith("finding_")
    assert finding.steps == ("Abrir funcionário", "Preencher telefone", "Salvar", "Recarregar")
    assert finding.preconditions == ("Usuário autenticado no tenant esperado",)
    assert [item.artifact_id for item in finding.evidence] == [shot]
    assert finding.evidence[0].relative_path == "evidence/shot.png"
    assert finding.tenant == history_context.profile.tenant_id
    assert finding.environment_url == history_context.profile.target_url
    assert finding.limitations == ("Somente no Chromium",)
    assert len(finding.fingerprint) == 64
    events = [e for e in store.list_events(run_id) if e.event_type == EventType.FINDING_SUBMITTED]
    assert len(events) == 1
    assert isinstance(events[0].payload, dict)
    assert events[0].payload["finding_id"] == finding.id
    assert store.list_findings(run_id) == [finding]


def finding_evidence(*ids: str) -> tuple[EvidenceReference, ...]:
    return tuple(EvidenceReference(item, EvidencePurpose.STATE) for item in ids)


def test_identical_finding_is_idempotent(history_context: ProjectContext) -> None:
    store, run_id = started(history_context)
    first = store.report_finding(run_id, draft(action_id="a"))
    second = store.report_finding(run_id, draft(action_id="b"))

    assert (second.created, second.finding.id) == (False, first.finding.id)
    assert count(store, "finding_steps") == 4


def test_agent_cannot_submit_confirmed_finding(history_context: ProjectContext) -> None:
    store, run_id = started(history_context)
    with pytest.raises(ReportingError) as info:
        store.report_finding(run_id, draft(classification=FindingClassification.CONFIRMED))
    assert info.value.code == "classification_not_allowed"
    assert count(store, "findings") == 0


def test_finding_rejects_cross_run_artifact(history_context: ProjectContext) -> None:
    store, run_id = started(history_context)
    foreign = screenshot(store, run_id)
    store.finish_run(run_id, RunStatus.COMPLETED)
    other = store.create_run(history_context, TextMission("Outra")).record.id

    for evidence in (finding_evidence(foreign), finding_evidence("art_unknown")):
        with pytest.raises(ReportingError) as info:
            store.report_finding(other, draft(evidence=evidence))
        assert info.value.detail == EVIDENCE_OUTSIDE_RUN
    assert count(store, "findings") == 0


def test_mission_criterion_source_must_reference_active_plan(
    history_context: ProjectContext,
) -> None:
    store, run_id = started(history_context)
    criterion = store.submit_mission_plan(run_id, plan()).plan.criteria[0].id
    with pytest.raises(ReportingError):
        store.report_finding(
            run_id,
            draft(expected_source=ExpectedSource.MISSION_CRITERION, expected_source_ref="x"),
        )

    submission = store.report_finding(
        run_id,
        draft(expected_source=ExpectedSource.MISSION_CRITERION, expected_source_ref=criterion),
    )
    assert submission.finding.expected_source_ref == criterion


def test_finding_event_and_evidence_are_atomic(
    history_context: ProjectContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, run_id = started(history_context)
    shot = screenshot(store, run_id)
    events_before = len(store.list_events(run_id))

    def broken(*args: object) -> None:
        raise RuntimeError("after evidence insert")

    with monkeypatch.context() as patch:
        patch.setattr(sql, "select_finding", broken)
        with pytest.raises(RuntimeError):
            store.report_finding(run_id, draft(evidence=finding_evidence(shot)))

    assert [count(store, table) for table in ("findings", "finding_steps", "finding_evidence")] == [
        0,
        0,
        0,
    ]
    assert len(store.list_events(run_id)) == events_before


def test_finding_rejected_after_run_ends(history_context: ProjectContext) -> None:
    store, run_id = started(history_context)
    store.finish_run(run_id, RunStatus.COMPLETED)
    with pytest.raises(RunNotActiveError):
        store.report_finding(run_id, draft())


def test_promotion_requires_evidence_and_reproduction(history_context: ProjectContext) -> None:
    store, run_id = started(history_context)
    bare = store.report_finding(run_id, draft(title="Sem evidência")).finding
    shot = screenshot(store, run_id)
    proven = store.report_finding(run_id, draft(evidence=finding_evidence(shot))).finding
    store.finish_run(run_id, RunStatus.COMPLETED)

    with pytest.raises(ReportingError) as info:
        store.promote_finding(bare.id)
    assert info.value.detail == CONFIRMED_NEEDS_EVIDENCE
    with pytest.raises(ReportingError) as info:
        store.promote_finding(proven.id)
    assert info.value.code == "reproduction_required"

    child = store.create_run(history_context, TextMission("Reproduzir")).record.id
    child_shot = screenshot(store, child, "repro")
    with pytest.raises(ReportingError):
        store.record_reproduction_attempt(
            proven.id, run_id, ReproductionOutcome.REPRODUCED, (), "claude-code"
        )
    attempt = store.record_reproduction_attempt(
        proven.id, child, ReproductionOutcome.REPRODUCED, (child_shot,), "codex"
    )
    promoted = store.promote_finding(proven.id)

    assert promoted.classification == FindingClassification.CONFIRMED
    assert promoted.version == 2
    assert promoted.reproduction_attempts == (attempt,)
    summary = next(item for item in store.list_runs() if item.record.id == run_id)
    assert summary.findings.confirmed == 1


def test_met_requires_positive_evidence(history_context: ProjectContext) -> None:
    store, run_id = started(history_context)
    criterion = store.submit_mission_plan(run_id, plan()).plan.criteria[0].id
    for evidence, note in (((), "persistiu"), ((screenshot(store, run_id),), " ")):
        with pytest.raises(ReportingError) as info:
            store.record_criterion_result(
                run_id,
                CriterionResultDraft(criterion, CriterionOutcome.MET, evidence, note, "observed"),
            )
        assert info.value.code == "positive_evidence_required"
    assert count(store, "criterion_results") == 0


def test_result_rejects_foreign_criterion(history_context: ProjectContext) -> None:
    store, run_id = started(history_context)
    criterion = store.submit_mission_plan(run_id, plan()).plan.criteria[0].id
    store.finish_run(run_id, RunStatus.COMPLETED)
    other = store.create_run(history_context, TextMission("Outra")).record.id
    store.submit_mission_plan(other, plan())

    with pytest.raises(ReportingError) as info:
        store.record_criterion_result(
            other,
            CriterionResultDraft(criterion, CriterionOutcome.NOT_MET, (), "falhou", "observed"),
        )
    assert info.value.code == "criterion_not_active"


def test_criterion_results_are_revisions_and_idempotent(history_context: ProjectContext) -> None:
    store, run_id = started(history_context)
    criterion = store.submit_mission_plan(run_id, plan()).plan.criteria[0].id
    shot = screenshot(store, run_id)
    blocked = CriterionResultDraft(criterion, CriterionOutcome.BLOCKED, (), "sem acesso", "lock")
    met = CriterionResultDraft(
        criterion, CriterionOutcome.MET, (shot,), "O valor persistiu após recarregar.", "observed"
    )

    first = store.record_criterion_result(run_id, blocked)
    assert store.record_criterion_result(run_id, blocked).created is False
    second = store.record_criterion_result(run_id, met)
    third = store.record_criterion_result(run_id, blocked)

    assert (first.result.revision, second.result.revision, third.result.revision) == (1, 2, 3)
    assert [item.outcome for item in store.list_criterion_results(run_id)] == [
        CriterionOutcome.BLOCKED
    ]
    assert len(store.list_criterion_results(run_id, current_only=False)) == 3
    with pytest.raises(sqlite3.IntegrityError):
        conn = sqlite3.connect(store.database_path)
        try:
            conn.execute("UPDATE criterion_results SET note = 'x'")
        finally:
            conn.close()


def test_seal_marks_missing_criteria_not_evaluated(history_context: ProjectContext) -> None:
    store, run_id = started(history_context)
    other = {"text": "E-mail inválido é rejeitado", "source": "mission_explicit"}
    criteria = store.submit_mission_plan(run_id, plan(CRITERION, other)).plan.criteria
    store.record_criterion_result(
        run_id,
        CriterionResultDraft(criteria[0].id, CriterionOutcome.NOT_MET, (), "vazio", "observed"),
    )

    sealed = store.seal_criterion_results(run_id)

    assert [(item.criterion_id, item.outcome, item.reason, item.source) for item in sealed] == [
        (criteria[1].id, CriterionOutcome.INCONCLUSIVE, "not_evaluated", ResultSource.SYSTEM)
    ]
    assert store.seal_criterion_results(run_id) == []
    assert {item.criterion_id for item in store.list_criterion_results(run_id)} == {
        criterion.id for criterion in criteria
    }
