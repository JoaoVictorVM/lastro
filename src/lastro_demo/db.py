"""Acesso ao SQLite do demo: esquema, conexões e transação de reset."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS clinics (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_clinics (
    user_id INTEGER NOT NULL REFERENCES users(id),
    clinic_id TEXT NOT NULL REFERENCES clinics(id),
    PRIMARY KEY (user_id, clinic_id)
);
CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clinic_id TEXT NOT NULL REFERENCES clinics(id),
    full_name TEXT NOT NULL,
    cpf TEXT NOT NULL,
    email TEXT NOT NULL,
    phone TEXT,
    role TEXT NOT NULL,
    hire_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_employees_clinic ON employees (clinic_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_employees_clinic_cpf ON employees (clinic_id, cpf);
"""

DATA_TABLES = ("employees", "user_clinics", "users", "clinics")


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def connection(path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def create_schema(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with connection(path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)


def clear_data(conn: sqlite3.Connection) -> None:
    for table in DATA_TABLES:
        conn.execute(f"DELETE FROM {table}")
    # Reinicia o autoincremento para que os ids do seed sejam iguais após cada reset.
    conn.execute("DELETE FROM sqlite_sequence WHERE name = ?", ("employees",))


def count_rows(path: Path) -> tuple[int, int]:
    """Clínicas e funcionários, lidos em modo somente leitura (usado pelo runner)."""
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        clinics = conn.execute("SELECT COUNT(*) FROM clinics").fetchone()[0]
        employees = conn.execute("SELECT COUNT(*) FROM employees").fetchone()[0]
    finally:
        conn.close()
    return int(clinics), int(employees)
