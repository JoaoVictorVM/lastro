from pathlib import Path

import pytest

from lastro.evaluation.models import Condition, EvalError, TrialRole
from lastro.evaluation.suites import (
    block_seed,
    expand_slots,
    load_suite,
    packaged_suite_names,
    parse_yaml,
    validate_suite,
)

MINIMAL = """schema_version: 1
id: local
conditions: [full]
missions:
  - id: crud
    file: mission.md
scenarios:
  - id: base
    mission: crud
    demo: {defects: [d02], scenarios: [], variant: base}
"""


def write_suite(folder: Path, text: str = MINIMAL, mission: str = "# Objetivo\n\nTeste.\n") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "mission.md").write_text(mission, encoding="utf-8")
    path = folder / "suite.yaml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("a: 1\na: 2\n", "duplicada"),
        ("base: &x {a: 1}\nother: *x\n", "aliases"),
        ("a: !!python/object:os.system x\n", "tags"),
        ("a: !custom 1\n", "tags"),
        ("a: 1\n---\nb: 2\n", "um documento"),
    ],
)
def test_rejects_duplicate_keys_aliases_and_unknown_fields(text: str, fragment: str) -> None:
    with pytest.raises(EvalError) as error:
        parse_yaml(text.encode())
    assert error.value.code == "invalid_suite"
    assert fragment in error.value.message


@pytest.mark.parametrize(
    "patch",
    [
        {"extra": 1},
        {"runs_per_condition": True},
        {"runs_per_condition": 0},
        {"conditions": ["full", "full"]},
        {"conditions": ["bogus"]},
        {"id": "Bad Id"},
        {"executor": "shell"},
    ],
)
def test_rejects_unknown_fields_and_invalid_values(patch: dict[str, object]) -> None:
    data = parse_yaml(MINIMAL.encode())
    with pytest.raises(EvalError) as error:
        validate_suite({**data, **patch})
    assert error.value.code == "invalid_suite"


def test_rejects_oversized_file() -> None:
    with pytest.raises(EvalError, match="1 MiB"):
        parse_yaml(b"a: " + b"x" * (1024 * 1024 + 1))


def test_rejects_resource_escape(tmp_path: Path) -> None:
    (tmp_path / "secret.md").write_text("# segredo", encoding="utf-8")
    path = write_suite(tmp_path / "suite", MINIMAL.replace("mission.md", "../secret.md"))
    with pytest.raises(EvalError, match=r"inválido|fora"):
        load_suite(str(path))
    absolute = write_suite(tmp_path / "abs", MINIMAL.replace("mission.md", "/etc/passwd"))
    with pytest.raises(EvalError, match="absoluto"):
        load_suite(str(absolute))


def test_local_suite_resolves_relative_to_its_root(tmp_path: Path) -> None:
    loaded = load_suite(str(write_suite(tmp_path / "suite")))
    assert loaded.mission_text("crud").startswith("# Objetivo")
    assert not loaded.root.packaged


def test_expands_six_conditions_five_repetitions() -> None:
    loaded = load_suite("demo-core")
    slots = expand_slots(loaded.spec)
    measured = [slot for slot in slots if slot.role == TrialRole.MEASURED]
    assert len(measured) == 30
    assert all(slot.applicable for slot in measured)
    for condition in Condition:
        assert sum(1 for slot in measured if slot.condition == condition) == 5
    assert [slot.ordinal for slot in slots] == list(range(1, 31))
    assert set(packaged_suite_names()) >= {"demo-core", "demo-scenarios", "demo-memory"}


def test_order_is_seeded_and_preserves_pairs() -> None:
    spec = load_suite("demo-memory").spec
    first, second = expand_slots(spec), expand_slots(spec)
    assert first == second
    other = expand_slots(spec.model_copy(update={"seed": spec.seed + 1}))
    assert [slot.condition for slot in other] != [slot.condition for slot in first]
    by_ordinal = {slot.ordinal: slot for slot in first}
    for slot in first:
        if slot.role == TrialRole.MEASURED:
            parent = by_ordinal[slot.parent_ordinal or 0]
            assert parent.role == TrialRole.WARMUP and parent.ordinal < slot.ordinal
            assert parent.repetition == slot.repetition and parent.scenario == slot.scenario
            assert parent.demo_seed == slot.demo_seed
    seeds = {slot.repetition: slot.demo_seed for slot in first}
    assert len(set(seeds.values())) == len(seeds)
    assert block_seed(11, 1) == seeds[1]


def test_baseline_scenarios_that_need_lastro_are_not_applicable() -> None:
    slots = expand_slots(load_suite("demo-scenarios").spec)
    gates = [slot for slot in slots if slot.scenario == "gate-approved"]
    assert {slot.applicable for slot in gates if slot.condition == Condition.BASELINE} == {False}
    assert {slot.applicable for slot in gates if slot.condition == Condition.FULL} == {True}
    stale = [slot for slot in slots if slot.scenario == "stale-knowledge"]
    assert not any(slot.applicable for slot in stale if slot.condition == Condition.NO_MEMORY)
