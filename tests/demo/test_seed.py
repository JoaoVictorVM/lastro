from collections import Counter
from pathlib import Path

import pytest

from lastro_demo import db
from lastro_demo.config import env_example_defaults, load_seed_users
from lastro_demo.flags import FlagState
from lastro_demo.seed import injection_names, load_seed, seed_employees
from lastro_demo.validation import NAME_MAX, ROLES, STATUSES, is_valid_cpf, only_digits


def seeded_db(tmp_path: Path, flags: FlagState) -> Path:
    path = tmp_path / "demo.sqlite3"
    db.create_schema(path)
    with db.connection(path) as conn, db.transaction(conn):
        load_seed(conn, load_seed_users({}), flags)
    return path


def cpfs_by_clinic(flags: FlagState) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for item in seed_employees(flags):
        result.setdefault(item.clinic_id, []).append(item.cpf)
    return result


def test_seed_counts(tmp_path: Path) -> None:
    path = seeded_db(tmp_path, FlagState.all_on())

    with db.connection(path) as conn:
        clinics = [row["id"] for row in conn.execute("SELECT id FROM clinics ORDER BY id")]
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        links = conn.execute("SELECT COUNT(*) FROM user_clinics").fetchone()[0]
        counts = {
            row[0]: row[1]
            for row in conn.execute("SELECT clinic_id, COUNT(*) FROM employees GROUP BY clinic_id")
        }

    assert clinics == ["aurora", "boreal"]
    assert users == 3
    assert links == 4
    assert counts == {"aurora": 20, "boreal": 20}
    assert db.count_rows(path) == (2, 40)


def test_seed_cpfs_valid_and_unique_per_clinic() -> None:
    for cpfs in cpfs_by_clinic(FlagState.all_off()).values():
        assert all(is_valid_cpf(cpf) for cpf in cpfs)
        assert len({only_digits(cpf) for cpf in cpfs}) == len(cpfs) == 20


def test_seed_fictitious_data_only() -> None:
    employees = seed_employees(FlagState.all_on())
    users = load_seed_users({})

    assert all(item.email.endswith("@example.com") for item in employees)
    assert all(user.email.endswith("@example.com") for user in users)
    assert all("falsa" in user.password for user in users)
    assert set(env_example_defaults()) >= {user.user_env for user in users}


def test_seed_field_values_are_valid() -> None:
    employees = seed_employees(FlagState.all_on())

    assert {item.role for item in employees} == set(ROLES)
    assert {item.status for item in employees} == set(STATUSES)
    assert Counter(item.status for item in employees)["active"] > 30
    assert all(3 <= len(item.full_name) <= NAME_MAX for item in employees)


def test_seed_trigger_records_present() -> None:
    clinics = cpfs_by_clinic(FlagState.all_off())
    employees = seed_employees(FlagState.all_off())
    aurora_digits = {only_digits(cpf) for cpf in clinics["aurora"]}
    boreal_only = [cpf for cpf in clinics["boreal"] if only_digits(cpf) not in aurora_digits]

    for clinic in ("aurora", "boreal"):
        assert any("'" in item.full_name for item in employees if item.clinic_id == clinic)
    assert any(51 <= len(item.full_name) <= 120 for item in employees)
    assert boreal_only
    assert any("." in cpf for cpf in clinics["aurora"])
    assert any(cpf.isdigit() for cpf in clinics["aurora"])


@pytest.mark.parametrize("flags", [FlagState.all_off(), FlagState.only("s01")])
def test_seed_s01_count_stable(tmp_path: Path, flags: FlagState) -> None:
    path = seeded_db(tmp_path, flags)

    with db.connection(path) as conn:
        names = [row[0] for row in conn.execute("SELECT full_name FROM employees")]
        counts = {
            row[0]: row[1]
            for row in conn.execute("SELECT clinic_id, COUNT(*) FROM employees GROUP BY clinic_id")
        }

    assert counts == {"aurora": 20, "boreal": 20}
    has_injection = any(name in injection_names() for name in names)
    assert has_injection == flags.scenario("s01")


def test_seed_reload_is_identical(tmp_path: Path) -> None:
    path = seeded_db(tmp_path, FlagState.all_off())
    with db.connection(path) as conn:
        first = [tuple(row) for row in conn.execute("SELECT * FROM employees ORDER BY id")]
        with db.transaction(conn):
            load_seed(conn, load_seed_users({}), FlagState.all_off())
        second = [tuple(row) for row in conn.execute("SELECT * FROM employees ORDER BY id")]

    assert first == second
