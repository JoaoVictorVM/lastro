import pytest

from lastro.mission import CharterInput, Heuristic, build_charter, timebox_limit

CHARTER = {
    "target": "employee registration",
    "resources": ["demo application", "mission criteria"],
    "information": "validation and persistence defects",
    "heuristics": ["boundary_values", "persistence_after_reload"],
}


def test_charter_has_canonical_format() -> None:
    charter = build_charter(CharterInput.model_validate(CHARTER))

    assert charter.statement == (
        "Explore employee registration with demo application, mission criteria "
        "to discover validation and persistence defects"
    )
    assert charter.heuristics == (Heuristic.BOUNDARY_VALUES, Heuristic.PERSISTENCE_AFTER_RELOAD)
    assert charter.as_json()["heuristics"] == ["boundary_values", "persistence_after_reload"]


def test_default_timebox_is_twenty() -> None:
    assert build_charter(CharterInput.model_validate(CHARTER)).timebox_minutes == 20


def test_timebox_cannot_exceed_run_limit() -> None:
    long = CharterInput.model_validate(CHARTER | {"timebox_minutes": 25})

    assert build_charter(long, max_minutes=25).timebox_minutes == 25
    with pytest.raises(ValueError, match="exceeds the run limit of 15"):
        build_charter(long, max_minutes=15)
    with pytest.raises(ValueError, match="run limit of 30"):
        build_charter(CharterInput.model_validate(CHARTER | {"timebox_minutes": 31}))
    assert timebox_limit({"max_minutes": 15}) == 15
    assert timebox_limit({}) == 30
    assert timebox_limit({"max_minutes": True}) == 30
