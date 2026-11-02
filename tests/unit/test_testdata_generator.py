import re
from datetime import UTC, date, datetime

import pytest

from lastro import testdata
from lastro_demo.validation import is_valid_cpf, is_valid_phone

RUN_ID = "run_20261101_120000_a1b2"
REFERENCE = date(2026, 11, 1)


def make(raw: dict[str, object], index: int = 1, run_id: str = RUN_ID) -> testdata.GeneratedData:
    return testdata.generate(
        testdata.parse_request(raw),
        run_id=run_id,
        generation_index=index,
        reference_date=REFERENCE,
    )


def test_employee_has_valid_cpf_marker_and_reserved_email() -> None:
    result = make({"entity": "employee"})

    assert isinstance(result, testdata.GeneratedEntity)
    values = result.values
    assert set(values) == {"name", "cpf", "email", "phone", "hire_date"}
    assert result.marker == "LASTRO-a1b2-"
    assert values["name"].startswith("LASTRO-a1b2-")
    assert values["email"] == f"lastro+{RUN_ID}@example.com"
    assert re.fullmatch(r"\d{3}\.\d{3}\.\d{3}-\d{2}", values["cpf"])
    assert is_valid_cpf(values["cpf"])
    assert is_valid_phone(values["phone"])
    assert date.fromisoformat(values["hire_date"]) < REFERENCE
    assert result.reference_date == "2026-11-01"
    assert result.generation_index == 1


@pytest.mark.parametrize("index", range(1, 60))
def test_invalid_cpf_changes_check_digits(index: int) -> None:
    employee = make({"entity": "employee", "variant": "invalid_cpf"}, index)
    isolated = make({"type": "cpf", "variant": "invalid"}, index)

    assert isinstance(employee, testdata.GeneratedEntity)
    assert isinstance(isolated, testdata.GeneratedValue)
    for value in (employee.values["cpf"], isolated.value):
        assert re.fullmatch(r"\d{3}\.\d{3}\.\d{3}-\d{2}", value)
        assert not is_valid_cpf(value)
    valid = make({"type": "cpf"}, index)
    assert isinstance(valid, testdata.GeneratedValue)
    assert is_valid_cpf(valid.value)


def test_same_run_order_reproduces_values() -> None:
    first = [make({"entity": "employee"}, 1), make({"type": "phone"}, 2)]
    second = [make({"entity": "employee"}, 1), make({"type": "phone"}, 2)]

    assert first == second


def test_successive_calls_differ() -> None:
    values = {make({"type": "cpf"}, index).model_dump()["value"] for index in range(1, 21)}

    assert len(values) == 20


def test_runs_have_independent_faker_instances() -> None:
    other = "run_20261101_120000_c3d4"
    alone = make({"entity": "employee"}, 1, other)
    make({"entity": "employee"}, 1)
    interleaved = make({"entity": "employee"}, 1, other)

    assert alone == interleaved
    assert alone != make({"entity": "employee"}, 1)


def test_date_variants_use_profile_timezone() -> None:
    late_utc = datetime(2026, 11, 2, 2, 30, tzinfo=UTC)

    assert testdata.reference_date(late_utc, "America/Sao_Paulo") == date(2026, 11, 1)
    assert testdata.reference_date(late_utc, "UTC") == date(2026, 11, 2)
    today = make({"type": "date"})
    past = make({"type": "date", "variant": "past"})
    future = make({"type": "date", "variant": "future"})
    assert isinstance(today, testdata.GeneratedValue)
    assert isinstance(past, testdata.GeneratedValue)
    assert isinstance(future, testdata.GeneratedValue)
    assert today.variant == "today"
    assert today.value == "2026-11-01"
    assert 1 <= (REFERENCE - date.fromisoformat(past.value)).days <= 365
    assert 1 <= (date.fromisoformat(future.value) - REFERENCE).days <= 365


@pytest.mark.parametrize(
    ("offset", "expected"), [(-1, "2026-02-28"), (0, "2026-03-01"), (1, "2026-03-02")]
)
def test_boundary_date_applies_minus_zero_plus_one(offset: int, expected: str) -> None:
    result = make(
        {
            "type": "date",
            "variant": "boundary",
            "boundary_date": "2026-03-01",
            "offset_days": offset,
        }
    )

    assert isinstance(result, testdata.GeneratedValue)
    assert result.value == expected


@pytest.mark.parametrize("length", [1, 2, 5, 37, 255, 500])
def test_text_has_exact_requested_length(length: int) -> None:
    result = make({"type": "text", "length": length})

    assert isinstance(result, testdata.GeneratedValue)
    assert len(result.value) == length
    assert result.value == result.value.strip()
    assert "<" not in result.value


def test_isolated_name_and_email_keep_marker() -> None:
    name = make({"type": "name"})
    email = make({"type": "email"})

    assert isinstance(name, testdata.GeneratedValue)
    assert isinstance(email, testdata.GeneratedValue)
    assert name.value.startswith("LASTRO-a1b2-")
    assert email.value.endswith("@example.com")
    assert "seed" not in name.model_dump()
