"""Regras de campo do funcionário, no comportamento correto, com mensagens em português."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

NAME_MIN = 3
NAME_MAX = 120

ROLES: dict[str, str] = {
    "recepcionista": "Recepcionista",
    "enfermeiro": "Enfermeiro",
    "medico": "Médico",
    "tecnico_enfermagem": "Técnico de Enfermagem",
    "administrativo": "Administrativo",
    "fisioterapeuta": "Fisioterapeuta",
}
STATUSES: dict[str, str] = {"active": "Ativo", "inactive": "Inativo"}
FIELDS = ("full_name", "cpf", "email", "phone", "role", "hire_date", "status")

MSG_NAME = f"O nome deve ter entre {NAME_MIN} e {NAME_MAX} caracteres."
MSG_CPF_INVALID = "CPF inválido."
MSG_CPF_DUPLICATE = "CPF já cadastrado nesta clínica."
MSG_EMAIL_REQUIRED = "O e-mail é obrigatório."
MSG_EMAIL_INVALID = "E-mail inválido."
MSG_PHONE = "Telefone inválido. Use o formato (11) 91234-5678 ou (11) 3456-7890."
MSG_ROLE_REQUIRED = "O perfil é obrigatório."
MSG_ROLE_INVALID = "Perfil inválido."
MSG_HIRE_DATE_REQUIRED = "A data de admissão é obrigatória."
MSG_HIRE_DATE_INVALID = "Data de admissão inválida."
MSG_HIRE_DATE_FUTURE = "A data de admissão não pode estar no futuro."
MSG_STATUS = "Status inválido."

_CPF_SHAPE = re.compile(r"^\d{3}\.?\d{3}\.?\d{3}-?\d{2}$")
_PHONE_SHAPE = re.compile(r"^(\(\d{2}\)|\d{2})\s?\d{4,5}[-\s]?\d{4}$")
_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def only_digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def is_valid_cpf(value: str) -> bool:
    raw = value.strip()
    if not _CPF_SHAPE.match(raw):
        return False
    digits = [int(char) for char in only_digits(raw)]
    if len(set(digits)) == 1:
        return False
    for size in (9, 10):
        total = sum(
            digit * weight
            for digit, weight in zip(digits[:size], range(size + 1, 1, -1), strict=True)
        )
        check = (total * 10) % 11 % 10
        if check != digits[size]:
            return False
    return True


def has_cpf_shape(value: str) -> bool:
    return bool(_CPF_SHAPE.match(value.strip()))


def is_valid_phone(value: str) -> bool:
    raw = value.strip()
    if not _PHONE_SHAPE.match(raw):
        return False
    digits = only_digits(raw)
    if len(digits) not in (10, 11) or digits[0] == "0" or digits[1] == "0":
        return False
    return len(digits) == 10 or digits[2] == "9"


def is_valid_email(value: str) -> bool:
    return bool(_EMAIL_SHAPE.match(value.strip()))


def name_error(value: str) -> str | None:
    length = len(value.strip())
    return None if NAME_MIN <= length <= NAME_MAX else MSG_NAME


def hire_date_error(value: str, *, today: date, allow_future: bool = False) -> str | None:
    raw = value.strip()
    if not raw:
        return MSG_HIRE_DATE_REQUIRED
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        return MSG_HIRE_DATE_INVALID
    if parsed > today and not allow_future:
        return MSG_HIRE_DATE_FUTURE
    return None


@dataclass(frozen=True)
class EmployeeInput:
    full_name: str
    cpf: str
    email: str
    phone: str | None
    role: str
    hire_date: str
    status: str

    @classmethod
    def from_form(cls, form: Mapping[str, str]) -> "EmployeeInput":
        phone = (form.get("phone") or "").strip()
        return cls(
            full_name=(form.get("full_name") or "").strip(),
            cpf=(form.get("cpf") or "").strip(),
            email=(form.get("email") or "").strip(),
            phone=phone or None,
            role=(form.get("role") or "").strip(),
            hire_date=(form.get("hire_date") or "").strip(),
            status=(form.get("status") or "active").strip(),
        )

    def as_form(self) -> dict[str, str]:
        return {
            "full_name": self.full_name,
            "cpf": self.cpf,
            "email": self.email,
            "phone": self.phone or "",
            "role": self.role,
            "hire_date": self.hire_date,
            "status": self.status,
        }


def validate_employee(
    data: EmployeeInput,
    *,
    today: date,
    check_cpf_digits: bool = True,
    allow_future_hire_date: bool = False,
) -> dict[str, str]:
    """Erros por campo; dicionário vazio quando o registro é válido."""
    errors: dict[str, str] = {}
    if message := name_error(data.full_name):
        errors["full_name"] = message
    cpf_ok = is_valid_cpf(data.cpf) if check_cpf_digits else has_cpf_shape(data.cpf)
    if not cpf_ok:
        errors["cpf"] = MSG_CPF_INVALID
    if not data.email:
        errors["email"] = MSG_EMAIL_REQUIRED
    elif not is_valid_email(data.email):
        errors["email"] = MSG_EMAIL_INVALID
    if data.phone is not None and not is_valid_phone(data.phone):
        errors["phone"] = MSG_PHONE
    if not data.role:
        errors["role"] = MSG_ROLE_REQUIRED
    elif data.role not in ROLES:
        errors["role"] = MSG_ROLE_INVALID
    if message := hire_date_error(data.hire_date, today=today, allow_future=allow_future_hire_date):
        errors["hire_date"] = message
    if data.status not in STATUSES:
        errors["status"] = MSG_STATUS
    return errors
