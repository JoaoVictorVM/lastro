import sqlite3
from typing import Any

import pytest

from lastro.browser.events import record_call
from lastro.browser.policy import Decision
from lastro.config.loader import ProjectContext
from lastro.history import (
    HistoryStore,
    MissionPlanError,
    PlanStatus,
    RunStatus,
    TextMission,
    open_history,
)
from lastro.history import mission_plans as sql
from lastro.mission import ClarificationAnswer, ClarificationInput, MissionPlanInput

PHONE = {"text": "O telefone salvo permanece após recarregar", "source": "mission_explicit"}
EMAIL = {"text": "E-mail inválido é rejeitado", "source": "mission_explicit"}


def plan(**overrides: Any) -> MissionPlanInput:
    data: dict[str, Any] = {
        "objectives": ["Validar o cadastro de funcionários"],
        "scope": ["Criar e editar funcionário"],
        "criteria": [PHONE],
        "gaps": [{"description": "Regra de CPF duplicado não informada", "blocking": False}],
        "out_of_scope": ["Excluir funcionários"],
        "charter": {
            "target": "employee registration",
            "resources": ["demo application"],
            "information": "persistence defects",
            "heuristics": ["persistence_after_reload"],
        },
    }
    data.update(overrides)
    return MissionPlanInput.model_validate(data)


def count(store: HistoryStore, table: str) -> int:
    conn = sqlite3.connect(store.database_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def started(history_context: ProjectContext) -> tuple[HistoryStore, str]:
    store = open_history(history_context)
    return store, store.create_run(history_context, TextMission("Explore")).record.id


def test_submit_plan_and_criteria_commit_atomically(
    history_context: ProjectContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, run_id = started(history_context)

    def broken(*args: object) -> None:
        raise RuntimeError("after criteria insert")

    with monkeypatch.context() as patch:
        patch.setattr(sql, "select_active_plan", broken)
        with pytest.raises(RuntimeError):
            store.submit_mission_plan(run_id, plan())
    assert (count(store, "criteria"), count(store, "mission_plans")) == (0, 0)

    submission = store.submit_mission_plan(run_id, plan())

    stored = submission.plan
    assert submission.created and stored.revision == 1
    assert stored.criteria[0].text == PHONE["text"]
    assert stored.criteria[0].id.startswith("criterion_")
    assert stored.gaps[0].id.startswith("gap_") and not stored.gaps[0].blocking
    assert stored.charter["statement"] == (
        "Explore employee registration with demo application to discover persistence defects"
    )
    assert stored.charter["timebox_minutes"] == 20
    assert stored.exploration_allowed
    assert store.mission_context(run_id).plan_status == PlanStatus.ACTIVE
    assert store.get_active_mission_plan(run_id) == stored


def test_identical_plan_is_idempotent(history_context: ProjectContext) -> None:
    store, run_id = started(history_context)
    first = store.submit_mission_plan(run_id, plan())

    again = store.submit_mission_plan(
        run_id, plan(objectives=["Validar  o cadastro de funcionários"])
    )

    assert not again.created and again.plan == first.plan
    assert (count(store, "criteria"), count(store, "mission_plans")) == (1, 1)


def test_revision_preserves_prior_plan(history_context: ProjectContext) -> None:
    store, run_id = started(history_context)
    store.submit_mission_plan(run_id, plan())

    with pytest.raises(MissionPlanError, match="requires revision_reason"):
        store.submit_mission_plan(run_id, plan(scope=["Criar funcionário"]))
    revised = store.submit_mission_plan(
        run_id, plan(scope=["Criar funcionário"], revision_reason="initial")
    ).plan
    record_call(store, run_id, "nav-1", "navigate", {}, Decision("allow", "read", ""))
    with pytest.raises(MissionPlanError) as error:
        store.submit_mission_plan(run_id, plan(scope=["Outro"], revision_reason="initial"))

    assert error.value.code == "revision_requires_clarification"
    plans = store.list_mission_plans(run_id)
    assert [item.revision for item in plans] == [1, 2]
    assert plans[0].scope == ("Criar e editar funcionário",)
    assert store.get_active_mission_plan(run_id) == revised


def test_revision_reuses_identical_criteria_and_adds_new(history_context: ProjectContext) -> None:
    store, run_id = started(history_context)
    blocking = [{"description": "Formato de telefone não informado", "blocking": True}]
    first = store.submit_mission_plan(run_id, plan(gaps=blocking)).plan
    assert store.mission_context(run_id).plan_status == PlanStatus.BLOCKED
    clarification, _ = store.request_clarification(
        run_id,
        ClarificationInput(
            gap_id=first.gaps[0].id,
            question="Qual formato de telefone?",
            why_it_matters="Define os valores de fronteira",
            options=["Celular com DDD", "Fixo"],
        ),
    )
    store.answer_clarification(clarification.id, ClarificationAnswer(answer="Celular com DDD"))
    clarified = {"text": "Telefone aceita celular com DDD", "source": "clarification"}

    second = store.submit_mission_plan(
        run_id,
        plan(
            criteria=[PHONE, clarified],
            gaps=[],
            revision_reason="clarification",
            clarification_id=clarification.id,
        ),
    ).plan

    assert second.criteria[0].id == first.criteria[0].id
    assert second.criteria[1].id != first.criteria[0].id
    assert second.exploration_allowed
    assert count(store, "criteria") == 2
    assert store.list_mission_plans(run_id)[0].criteria == first.criteria


def test_cross_run_references_rejected(history_context: ProjectContext) -> None:
    store, run_id = started(history_context)
    blocking = [{"description": "Formato de telefone não informado", "blocking": True}]
    first = store.submit_mission_plan(run_id, plan(gaps=blocking)).plan
    clarification, _ = store.request_clarification(
        run_id,
        ClarificationInput(
            gap_id=first.gaps[0].id,
            question="Qual formato?",
            why_it_matters="Fronteiras",
            options=["A", "B"],
        ),
    )
    store.answer_clarification(clarification.id, ClarificationAnswer(answer="A"))
    store.finish_run(run_id, RunStatus.COMPLETED)
    other = store.create_run(history_context, TextMission("Outra")).record.id
    store.submit_mission_plan(other, plan())

    with pytest.raises(MissionPlanError) as error:
        store.submit_mission_plan(
            other,
            plan(gaps=[], revision_reason="clarification", clarification_id=clarification.id),
        )
    assert error.value.code == "revision_requires_clarification"
    with pytest.raises(MissionPlanError) as gap_error:
        store.check_blocking_gap(other, first.gaps[0].id)
    assert gap_error.value.code == "invalid_reference"
    with pytest.raises(MissionPlanError):
        store.submit_mission_plan(run_id, plan(scope=["x"], revision_reason="initial"))


def test_timebox_beyond_run_limit_is_rejected_without_persistence(
    history_context: ProjectContext,
) -> None:
    store, run_id = started(history_context)
    charter = plan().charter.model_dump(mode="json") | {"timebox_minutes": 25}

    with pytest.raises(MissionPlanError, match="exceeds the run limit of 15"):
        store.submit_mission_plan(run_id, plan(charter=charter), max_minutes=15)
    assert count(store, "mission_plans") == 0
