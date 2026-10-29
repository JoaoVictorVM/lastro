from datetime import date, timedelta

import pytest

from lastro_demo.validation import (
    MSG_CPF_INVALID,
    MSG_EMAIL_INVALID,
    MSG_EMAIL_REQUIRED,
    MSG_HIRE_DATE_FUTURE,
    MSG_NAME,
    MSG_PHONE,
    MSG_ROLE_INVALID,
    MSG_STATUS,
    ROLES,
    EmployeeInput,
    hire_date_error,
    is_valid_cpf,
    is_valid_email,
    is_valid_phone,
    name_error,
    only_digits,
    validate_employee,
)

TODAY = date(2026, 3, 15)


def valid_input(**overrides: str) -> EmployeeInput:
    form = {
        "full_name": "Fulana de Tal",
        "cpf": "529.982.247-25",
        "email": "fulana@example.com",
        "phone": "(11) 91234-5678",
        "role": "enfermeiro",
        "hire_date": "2020-01-10",
        "status": "active",
    }
    form.update(overrides)
    return EmployeeInput.from_form(form)


@pytest.mark.parametrize("cpf", ["529.982.247-25", "52998224725", "111.444.777-35", "11144477735"])
def test_cpf_valid_digits_accepted(cpf: str) -> None:
    assert is_valid_cpf(cpf)


@pytest.mark.parametrize(
    "cpf",
    [
        "529.982.247-26",
        "52998224715",
        "111.111.111-11",
        "00000000000",
        "5299822472",
        "529982247255",
        "529.982.247",
        "abc.def.ghi-jk",
        "",
        "529-982-247.25",
    ],
)
def test_cpf_invalid_digits_rejected(cpf: str) -> None:
    assert not is_valid_cpf(cpf)


@pytest.mark.parametrize(
    "phone",
    ["(11) 91234-5678", "11912345678", "(21) 3456-7890", "2134567890", "11 91234-5678"],
)
def test_phone_brazilian_formats(phone: str) -> None:
    assert is_valid_phone(phone)


@pytest.mark.parametrize(
    "phone", ["1234-5678", "(01) 91234-5678", "(11) 81234-56789", "11812345678", "telefone", "+55"]
)
def test_phone_invalid_formats_rejected(phone: str) -> None:
    assert not is_valid_phone(phone)


def test_name_length_bounds() -> None:
    assert name_error("ab") == MSG_NAME
    assert name_error("abc") is None
    assert name_error("a" * 120) is None
    assert name_error("a" * 121) == MSG_NAME
    assert name_error("   ab   ") == MSG_NAME


def test_hire_date_future_rejected() -> None:
    assert hire_date_error(TODAY.isoformat(), today=TODAY) is None
    assert hire_date_error((TODAY - timedelta(days=1)).isoformat(), today=TODAY) is None
    tomorrow = (TODAY + timedelta(days=1)).isoformat()
    assert hire_date_error(tomorrow, today=TODAY) == MSG_HIRE_DATE_FUTURE
    assert hire_date_error(tomorrow, today=TODAY, allow_future=True) is None
    assert hire_date_error("", today=TODAY) is not None
    assert hire_date_error("15/03/2026", today=TODAY) is not None


def test_email_format() -> None:
    assert is_valid_email("alguem@example.com")
    assert not is_valid_email("alguem@")
    assert not is_valid_email("sem arroba.example.com")


def test_validate_employee_valid_has_no_errors() -> None:
    assert validate_employee(valid_input(), today=TODAY) == {}
    assert validate_employee(valid_input(phone=""), today=TODAY) == {}


@pytest.mark.parametrize("role", list(ROLES))
def test_validate_employee_accepts_every_role(role: str) -> None:
    assert validate_employee(valid_input(role=role), today=TODAY) == {}


def test_validate_employee_reports_each_field() -> None:
    data = valid_input(
        full_name="ab",
        cpf="123",
        email="",
        phone="999",
        role="astronauta",
        hire_date="2099-01-01",
        status="removido",
    )

    errors = validate_employee(data, today=TODAY)

    assert errors == {
        "full_name": MSG_NAME,
        "cpf": MSG_CPF_INVALID,
        "email": MSG_EMAIL_REQUIRED,
        "phone": MSG_PHONE,
        "role": MSG_ROLE_INVALID,
        "hire_date": MSG_HIRE_DATE_FUTURE,
        "status": MSG_STATUS,
    }
    assert validate_employee(valid_input(email="x@"), today=TODAY)["email"] == MSG_EMAIL_INVALID


def test_validate_employee_can_skip_check_digits_only() -> None:
    wrong_digits = valid_input(cpf="529.982.247-26")

    assert validate_employee(wrong_digits, today=TODAY)
    assert validate_employee(wrong_digits, today=TODAY, check_cpf_digits=False) == {}
    assert validate_employee(valid_input(cpf="123"), today=TODAY, check_cpf_digits=False)


def test_only_digits() -> None:
    assert only_digits("529.982.247-25") == "52998224725"
