"""Carga do seed: clínicas, usuários de teste e 40 funcionários fictícios."""

import sqlite3
from dataclasses import dataclass
from functools import cache
from typing import Any

from lastro_demo.config import SeedUser, read_seed_json
from lastro_demo.db import clear_data
from lastro_demo.flags import FlagState
from lastro_demo.security import seed_password_hash

SEED_TIMESTAMP = "2024-01-02T09:00:00"
INJECTION_FIELD = "injection_name"


@dataclass(frozen=True)
class SeedEmployee:
    clinic_id: str
    full_name: str
    cpf: str
    email: str
    phone: str | None
    role: str
    hire_date: str
    status: str


@dataclass(frozen=True)
class SeedClinic:
    id: str
    name: str


@cache
def _raw_employees() -> dict[str, list[dict[str, Any]]]:
    return read_seed_json("employees.json")


@cache
def seed_clinics() -> tuple[SeedClinic, ...]:
    return tuple(
        SeedClinic(id=str(item["id"]), name=str(item["name"]))
        for item in read_seed_json("clinics.json")["clinics"]
    )


def seed_employees(flags: FlagState) -> list[SeedEmployee]:
    """Funcionários na ordem de inserção; o cenário s01 troca só o nome de um registro."""
    injection_on = flags.scenario("s01")
    employees: list[SeedEmployee] = []
    for clinic_id, rows in _raw_employees().items():
        for row in rows:
            name = row.get(INJECTION_FIELD) if injection_on else None
            employees.append(
                SeedEmployee(
                    clinic_id=clinic_id,
                    full_name=str(name or row["full_name"]),
                    cpf=str(row["cpf"]),
                    email=str(row["email"]),
                    phone=row["phone"],
                    role=str(row["role"]),
                    hire_date=str(row["hire_date"]),
                    status=str(row["status"]),
                )
            )
    return employees


def injection_names() -> tuple[str, ...]:
    return tuple(
        str(row[INJECTION_FIELD])
        for rows in _raw_employees().values()
        for row in rows
        if INJECTION_FIELD in row
    )


def load_seed(conn: sqlite3.Connection, users: tuple[SeedUser, ...], flags: FlagState) -> None:
    """Apaga e recria todos os dados; o chamador controla a transação."""
    clear_data(conn)
    conn.executemany(
        "INSERT INTO clinics (id, name) VALUES (?, ?)",
        [(clinic.id, clinic.name) for clinic in seed_clinics()],
    )
    for number, user in enumerate(users, start=1):
        conn.execute(
            "INSERT INTO users (id, email, display_name, password_hash) VALUES (?, ?, ?, ?)",
            (number, user.email, user.display_name, seed_password_hash(user.password)),
        )
        conn.executemany(
            "INSERT INTO user_clinics (user_id, clinic_id) VALUES (?, ?)",
            [(number, clinic_id) for clinic_id in user.clinics],
        )
    conn.executemany(
        "INSERT INTO employees (clinic_id, full_name, cpf, email, phone, role, hire_date, status,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                item.clinic_id,
                item.full_name,
                item.cpf,
                item.email,
                item.phone,
                item.role,
                item.hire_date,
                item.status,
                SEED_TIMESTAMP,
                SEED_TIMESTAMP,
            )
            for item in seed_employees(flags)
        ],
    )
