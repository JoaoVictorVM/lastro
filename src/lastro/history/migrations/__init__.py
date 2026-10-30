"""Migrations versionadas: ordem, checksum e uma transação por versão.

Migrations publicadas são imutáveis; mudanças de schema entram sempre como uma versão nova.
"""

import hashlib
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from types import ModuleType

from lastro.history.database import Database
from lastro.history.errors import MigrationError
from lastro.history.migrations import v001_initial
from lastro.timeutil import utc_now_iso

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER NOT NULL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    checksum_sha256 TEXT NOT NULL
        CHECK (length(checksum_sha256) = 64 AND checksum_sha256 NOT GLOB '*[^0-9a-f]*'),
    applied_at TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        return hashlib.sha256("\n;\n".join(self.statements).encode("utf-8")).hexdigest()

    @classmethod
    def from_module(cls, module: ModuleType) -> "Migration":
        return cls(version=module.VERSION, name=module.NAME, statements=tuple(module.STATEMENTS))


MIGRATIONS: tuple[Migration, ...] = (Migration.from_module(v001_initial),)


def _applied(conn: sqlite3.Connection) -> dict[int, tuple[str, str]]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if exists is None:
        return {}
    rows = conn.execute("SELECT version, name, checksum_sha256 FROM schema_migrations").fetchall()
    return {int(row["version"]): (str(row["name"]), str(row["checksum_sha256"])) for row in rows}


def _verify(applied: dict[int, tuple[str, str]], migrations: Sequence[Migration]) -> None:
    known = {migration.version: migration for migration in migrations}
    for version, (name, checksum) in sorted(applied.items()):
        migration = known.get(version)
        if migration is None:
            raise MigrationError(version, "versão desconhecida por esta instalação do Lastro")
        if migration.name != name or migration.checksum != checksum:
            raise MigrationError(version, "checksum diferente da migration publicada")


def _apply_one(conn: sqlite3.Connection, migration: Migration) -> None:
    conn.execute(_BOOTSTRAP)
    for statement in migration.statements:
        conn.execute(statement)
    conn.execute(
        "INSERT INTO schema_migrations (version, name, checksum_sha256, applied_at) "
        "VALUES (?, ?, ?, ?)",
        (migration.version, migration.name, migration.checksum, utc_now_iso()),
    )


def apply_migrations(database: Database, migrations: Sequence[Migration] = MIGRATIONS) -> list[int]:
    """Aplica as versões pendentes em ordem; uma falha desfaz a versão inteira e interrompe."""
    ordered = sorted(migrations, key=lambda migration: migration.version)
    applied = database.read(_applied)
    _verify(applied, ordered)
    done: list[int] = []
    for migration in ordered:
        if migration.version in applied:
            continue

        def apply(conn: sqlite3.Connection, migration: Migration = migration) -> bool:
            # Outro processo pode ter aplicado a mesma versão enquanto esperávamos o lock.
            if migration.version in _applied(conn):
                return False
            _apply_one(conn, migration)
            return True

        try:
            if database.write(apply):
                done.append(migration.version)
        except sqlite3.Error as exc:
            raise MigrationError(migration.version, str(exc)) from exc
    return done


def schema_version(database: Database) -> int:
    applied = database.read(_applied)
    return max(applied, default=0)
