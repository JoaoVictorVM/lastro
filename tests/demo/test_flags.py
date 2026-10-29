import pytest

from lastro_demo.flags import (
    DEFECT_IDS,
    DEFECTS_ENV,
    SCENARIO_IDS,
    SCENARIOS_ENV,
    VARIANT_ENV,
    FlagState,
    UnknownFlagError,
    UnknownVariantError,
    parse_flag_list,
    validate_defects,
    validate_scenarios,
    validate_variant,
)


def test_registry_is_closed_and_opaque() -> None:
    assert DEFECT_IDS == ("d01", "d02", "d03", "d04", "d05", "d06", "d07", "d08", "d09", "d10")
    assert SCENARIO_IDS == ("s01", "s02")


def test_parse_missing_means_all_active() -> None:
    assert parse_flag_list(None, DEFECT_IDS) == frozenset(DEFECT_IDS)
    assert parse_flag_list("all", DEFECT_IDS) == frozenset(DEFECT_IDS)


@pytest.mark.parametrize("raw", ["", "none", "NONE", " , "])
def test_parse_empty_or_none_disables_all(raw: str) -> None:
    assert parse_flag_list(raw, DEFECT_IDS) == frozenset()


def test_parse_accepts_separators_and_case() -> None:
    assert parse_flag_list("D01, d04;d10 d02", DEFECT_IDS) == {"d01", "d02", "d04", "d10"}


def test_parse_unknown_raises_with_ids() -> None:
    with pytest.raises(UnknownFlagError) as info:
        parse_flag_list("d01,d11,x", DEFECT_IDS)

    assert info.value.unknown == ("d11", "x")
    assert "d11" in str(info.value)


def test_defects_and_scenarios_do_not_mix() -> None:
    with pytest.raises(UnknownFlagError):
        validate_defects(["s01"])
    with pytest.raises(UnknownFlagError):
        validate_scenarios(["d01"])


def test_variant_only_base() -> None:
    assert validate_variant("base") == "base"
    with pytest.raises(UnknownVariantError):
        validate_variant("campo-alternativo")


def test_from_env_defaults_to_all_on() -> None:
    state = FlagState.from_env({})

    assert state == FlagState.all_on()
    assert state.variant == "base"


def test_from_env_reads_each_channel() -> None:
    state = FlagState.from_env({DEFECTS_ENV: "d03,d08", SCENARIOS_ENV: "none", VARIANT_ENV: ""})

    assert state.sorted_defects() == ["d03", "d08"]
    assert state.sorted_scenarios() == []
    assert state.defect("d03")
    assert not state.defect("d01")
    assert not state.scenario("s01")


def test_from_env_rejects_unknown_variant() -> None:
    with pytest.raises(UnknownVariantError):
        FlagState.from_env({VARIANT_ENV: "v2"})


def test_only_builds_isolated_state() -> None:
    state = FlagState.only("d02", "s02")

    assert state.defects == {"d02"}
    assert state.scenarios == {"s02"}
    assert FlagState.all_off().defects == frozenset()
