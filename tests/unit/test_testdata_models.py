import pytest

from lastro import testdata


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"entity": "employee", "type": "cpf"},
        {"entity": "employee", "length": 10},
        {"entity": "employee", "boundary_date": "2026-11-01", "offset_days": 0},
    ],
)
def test_entity_and_type_are_exclusive(raw: dict[str, object]) -> None:
    with pytest.raises(testdata.TestDataError):
        testdata.parse_request(raw)


def test_unsupported_type_message_lists_supported() -> None:
    with pytest.raises(testdata.TestDataError) as error:
        testdata.parse_request({"type": "passport"})

    assert str(error.value) == (
        "Unsupported test data type 'passport'. Supported: name, cpf, email, phone, date, text."
    )
    with pytest.raises(testdata.TestDataError, match="Supported: employee"):
        testdata.parse_request({"entity": "customer"})


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ({"entity": "employee", "variant": "invalid"}, "variant"),
        ({"type": "cpf", "variant": "invalid_cpf"}, "variant"),
        ({"type": "phone", "variant": "invalid"}, "variant"),
        ({"type": "text"}, "length"),
        ({"type": "text", "length": 0}, "length"),
        ({"type": "text", "length": 501}, "length"),
        ({"type": "name", "length": 3}, "length"),
        ({"type": "date", "variant": "boundary", "offset_days": 1}, "boundary_date"),
        (
            {
                "type": "date",
                "variant": "boundary",
                "boundary_date": "2026-02-30",
                "offset_days": 0,
            },
            "boundary_date",
        ),
        (
            {"type": "date", "variant": "boundary", "boundary_date": "20260301", "offset_days": 0},
            "boundary_date",
        ),
        (
            {
                "type": "date",
                "variant": "boundary",
                "boundary_date": "2026-03-01",
                "offset_days": 2,
            },
            "offset_days",
        ),
        ({"type": "date", "boundary_date": "2026-03-01"}, "boundary"),
        ({"type": "cpf", "unexpected": True}, "unexpected"),
        ({"type": 1}, "type"),
        ({"type": "text", "length": "10"}, "length"),
    ],
)
def test_invalid_variant_arguments_rejected(raw: dict[str, object], fragment: str) -> None:
    with pytest.raises(testdata.TestDataError, match=fragment):
        testdata.parse_request(raw)


def test_defaults_resolve_variant() -> None:
    assert testdata.parse_request({"entity": "employee"}).variant == "valid"
    assert testdata.parse_request({"type": "date"}).variant == "today"
    assert testdata.parse_request({"type": "text", "length": 3}).variant == "valid"
