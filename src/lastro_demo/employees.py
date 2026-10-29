"""Domínio de funcionários: toda consulta e escrita é filtrada pelo tenant ativo."""

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime

from markupsafe import Markup

from lastro_demo.runtime import DemoRuntime
from lastro_demo.validation import (
    MSG_CPF_DUPLICATE,
    ROLES,
    STATUSES,
    EmployeeInput,
    only_digits,
    validate_employee,
)

SORT_KEYS = ("name", "role", "hire_date", "status")
DEFAULT_SORT = "name"
DIRECTIONS = ("asc", "desc")

COLUMNS = "id, clinic_id, full_name, cpf, email, phone, role, hire_date, status"
CPF_LENGTH = 11
_CPF_QUERY = re.compile(r"^[\d.\-\s]+$")


@dataclass(frozen=True)
class Employee:
    id: int
    clinic_id: str
    full_name: str
    cpf: str
    email: str
    phone: str | None
    role: str
    hire_date: str
    status: str

    @property
    def role_label(self) -> str:
        return ROLES.get(self.role, self.role)

    @property
    def status_label(self) -> str:
        return STATUSES.get(self.status, self.status)

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

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Employee":
        return cls(
            id=int(row["id"]),
            clinic_id=str(row["clinic_id"]),
            full_name=str(row["full_name"]),
            cpf=str(row["cpf"]),
            email=str(row["email"]),
            phone=None if row["phone"] is None else str(row["phone"]),
            role=str(row["role"]),
            hire_date=str(row["hire_date"]),
            status=str(row["status"]),
        )


@dataclass(frozen=True)
class SaveOutcome:
    """Resultado de criar ou editar: o registro exibido, erros por campo e o status HTTP."""

    employee: Employee | None
    errors: dict[str, str]
    status_code: int = 200

    @property
    def ok(self) -> bool:
        return not self.errors and self.employee is not None


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def normalize_sort(sort: str | None, direction: str | None) -> tuple[str, str]:
    key = sort if sort in SORT_KEYS else DEFAULT_SORT
    order = direction if direction in DIRECTIONS else "asc"
    return key, order


def _sort_value(employee: Employee, key: str) -> tuple[str, ...]:
    if key == "role":
        return (_fold(employee.role_label),)
    if key == "hire_date":
        return (employee.hire_date,)
    if key == "status":
        return (employee.status,)
    return (_fold(employee.full_name),)


def _matches(employee: Employee, query: str) -> bool:
    folded = _fold(query)
    if folded in _fold(employee.full_name) or folded in employee.email.casefold():
        return True
    digits = only_digits(query)
    return is_cpf_query(query) and len(digits) >= 3 and digits in only_digits(employee.cpf)


def is_cpf_query(query: str) -> bool:
    return bool(_CPF_QUERY.match(query.strip()))


def is_exact_cpf_query(query: str) -> bool:
    return is_cpf_query(query) and len(only_digits(query)) == CPF_LENGTH


def get_employee(conn: sqlite3.Connection, tenant_id: str, employee_id: int) -> Employee | None:
    row = conn.execute(
        f"SELECT {COLUMNS} FROM employees WHERE id = ? AND clinic_id = ?",
        (employee_id, tenant_id),
    ).fetchone()
    return None if row is None else Employee.from_row(row)


def list_employees(
    conn: sqlite3.Connection,
    runtime: DemoRuntime,
    tenant_id: str,
    *,
    query: str = "",
    sort: str | None = None,
    direction: str | None = None,
) -> list[Employee]:
    key, order = normalize_sort(sort, direction)
    rows = conn.execute(
        f"SELECT {COLUMNS} FROM employees WHERE clinic_id = ? ORDER BY id", (tenant_id,)
    ).fetchall()
    employees = [Employee.from_row(row) for row in rows]
    query = query.strip()
    if query:
        employees = [item for item in employees if _matches(item, query)]
    return sorted(employees, key=lambda item: _sort_value(item, key), reverse=order == "desc")


def _cpf_taken(conn: sqlite3.Connection, tenant_id: str, cpf: str, exclude_id: int | None) -> bool:
    rows = conn.execute(
        "SELECT id, cpf FROM employees WHERE clinic_id = ?", (tenant_id,)
    ).fetchall()
    digits = only_digits(cpf)
    return any(
        only_digits(str(row["cpf"])) == digits and int(row["id"]) != exclude_id for row in rows
    )


def _shown(employee_id: int, tenant_id: str, data: EmployeeInput) -> Employee:
    return Employee(
        id=employee_id,
        clinic_id=tenant_id,
        full_name=data.full_name,
        cpf=data.cpf,
        email=data.email,
        phone=data.phone,
        role=data.role,
        hire_date=data.hire_date,
        status=data.status,
    )


def create_employee(
    conn: sqlite3.Connection,
    runtime: DemoRuntime,
    tenant_id: str,
    data: EmployeeInput,
    *,
    today: date,
) -> SaveOutcome:
    errors = validate_employee(data, today=today)
    if "cpf" not in errors and _cpf_taken(conn, tenant_id, data.cpf, None):
        errors["cpf"] = MSG_CPF_DUPLICATE
    if errors:
        return SaveOutcome(None, errors)
    stamp = _now()
    cursor = conn.execute(
        "INSERT INTO employees (clinic_id, full_name, cpf, email, phone, role, hire_date, status,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tenant_id,
            data.full_name,
            data.cpf,
            data.email,
            data.phone,
            data.role,
            data.hire_date,
            data.status,
            stamp,
            stamp,
        ),
    )
    return SaveOutcome(_shown(int(cursor.lastrowid or 0), tenant_id, data), {})


def update_employee(
    conn: sqlite3.Connection,
    runtime: DemoRuntime,
    tenant_id: str,
    current: Employee,
    data: EmployeeInput,
    *,
    today: date,
) -> SaveOutcome:
    errors = validate_employee(data, today=today)
    if "cpf" not in errors and _cpf_taken(conn, tenant_id, data.cpf, current.id):
        errors["cpf"] = MSG_CPF_DUPLICATE
    if errors:
        return SaveOutcome(None, errors)
    conn.execute(
        "UPDATE employees SET full_name = ?, cpf = ?, email = ?, phone = ?, role = ?,"
        " hire_date = ?, status = ?, updated_at = ? WHERE id = ? AND clinic_id = ?",
        (
            data.full_name,
            data.cpf,
            data.email,
            data.phone,
            data.role,
            data.hire_date,
            data.status,
            _now(),
            current.id,
            tenant_id,
        ),
    )
    return SaveOutcome(_shown(current.id, tenant_id, data), {})


def script_name_literal(name: str, runtime: DemoRuntime) -> Markup:
    """Nome como literal JavaScript seguro para um trecho de script da página de edição."""
    encoded = json.dumps(name, ensure_ascii=False)
    for char, escape in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"), ("'", "\\u0027")):
        encoded = encoded.replace(char, escape)
    return Markup(encoded)


def deactivate_employee(conn: sqlite3.Connection, tenant_id: str, employee_id: int) -> bool:
    cursor = conn.execute(
        "UPDATE employees SET status = 'inactive', updated_at = ? WHERE id = ? AND clinic_id = ?",
        (_now(), employee_id, tenant_id),
    )
    return cursor.rowcount > 0
