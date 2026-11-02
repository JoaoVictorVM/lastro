from typing import Any

import pytest
from pydantic import ValidationError

from lastro.mission import CriterionSource, HeuristicCheckInput, MissionPlanInput


def plan(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "objectives": ["Validar o cadastro de funcionários"],
        "scope": [],
        "criteria": [],
        "gaps": [],
        "out_of_scope": [],
        "charter": {
            "target": "employee registration",
            "resources": ["demo application"],
            "information": "validation defects",
            "heuristics": ["boundary_values"],
        },
    }
    data.update(overrides)
    return data


def test_plan_requires_objective_and_all_lists() -> None:
    assert MissionPlanInput.model_validate(plan()).gaps == []

    with pytest.raises(ValidationError):
        MissionPlanInput.model_validate(plan(objectives=[]))
    for field in ("scope", "criteria", "gaps", "out_of_scope", "charter"):
        data = plan()
        del data[field]
        with pytest.raises(ValidationError):
            MissionPlanInput.model_validate(data)
    with pytest.raises(ValidationError):
        MissionPlanInput.model_validate(plan(extra="x"))


def test_schema_limits_and_deduplication() -> None:
    parsed = MissionPlanInput.model_validate(
        plan(objectives=["Validar  o cadastro", "Validar o cadastro ", "Outro"])
    )
    assert parsed.objectives == ["Validar o cadastro", "Outro"]

    with pytest.raises(ValidationError):
        MissionPlanInput.model_validate(plan(objectives=[f"o{i}" for i in range(11)]))
    with pytest.raises(ValidationError):
        MissionPlanInput.model_validate(plan(scope=["x" * 1001]))
    with pytest.raises(ValidationError):
        MissionPlanInput.model_validate(plan(scope=["   "]))
    with pytest.raises(ValidationError):
        MissionPlanInput.model_validate(
            plan(gaps=[{"description": f"g{i}", "blocking": False} for i in range(21)])
        )
    duplicated = {"text": "O telefone persiste", "source": "mission_explicit"}
    with pytest.raises(ValidationError, match="criteria must not repeat"):
        MissionPlanInput.model_validate(plan(criteria=[duplicated, duplicated]))
    charter = plan()["charter"] | {"heuristics": ["boundary_values", "boundary_values"]}
    with pytest.raises(ValidationError, match="heuristics must not repeat"):
        MissionPlanInput.model_validate(plan(charter=charter))
    with pytest.raises(ValidationError):
        MissionPlanInput.model_validate(plan(charter=plan()["charter"] | {"heuristics": []}))


def test_criterion_sources_are_explicit() -> None:
    parsed = MissionPlanInput.model_validate(
        plan(criteria=[{"text": "Telefone persiste", "source": "clarification"}])
    )
    assert parsed.criteria[0].source == CriterionSource.CLARIFICATION

    for source in ("heuristic", "observation", "inferred"):
        with pytest.raises(ValidationError):
            MissionPlanInput.model_validate(
                plan(criteria=[{"text": "Telefone persiste", "source": source}])
            )
    with pytest.raises(ValidationError, match="requires clarification_id"):
        MissionPlanInput.model_validate(plan(revision_reason="clarification"))
    with pytest.raises(ValidationError, match="requires revision_reason"):
        MissionPlanInput.model_validate(plan(clarification_id="clar_1"))


def test_check_requires_actions_and_known_heuristic() -> None:
    base = {"heuristic": "persistence_after_reload", "outcome": "failed", "action_ids": ["a"]}
    assert HeuristicCheckInput.model_validate(base).note == ""

    for change in (
        {"action_ids": []},
        {"action_ids": ["a", "a"]},
        {"action_ids": [str(i) for i in range(21)]},
        {"heuristic": "gut_feeling"},
        {"outcome": "met"},
        {"note": "x" * 2001},
    ):
        with pytest.raises(ValidationError):
            HeuristicCheckInput.model_validate(base | change)
