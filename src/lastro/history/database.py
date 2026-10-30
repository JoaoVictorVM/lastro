"""Acesso ao SQLite: conexões, pragmas, transações explícitas e retry limitado de lock."""

import sqlite3
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from lastro.history.errors import HistoryUnavailableError

LOCK_TIMEOUT_SECONDS = 5.0
# Uma tentativa inicial e até três repetições, com estes atrasos antes de cada repetição.
LOCK_RETRY_DELAYS = (0.25, 0.5, 1.0)

type Connector = Callable[..., sqlite3.Connection]


def db_timestamp(moment: datetime) -> str:
    """UTC com largura fixa, para que comparar texto no SQLite equivalha a comparar instantes."""
    if moment.tzinfo is None:
        raise ValueError("datetime sem fuso não pode ser convertido para UTC com segurança")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class RetryPolicy:
    timeout_seconds: float = LOCK_TIMEOUT_SECONDS
    delays: tuple[float, ...] = LOCK_RETRY_DELAYS
    sleep: Callable[[float], None] = time.sleep

    @property
    def max_attempts(self) -> int:
        return 1 + len(self.delays)


def is_lock_error(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    message = str(exc).lower()
    return "locked" in message or "busy" in message


class Database:
    """Abre uma conexão curta por operação, para nunca manter lock nem handle aberto entre usos."""

    def __init__(
        self,
        path: Path,
        policy: RetryPolicy | None = None,
        connector: Connector = sqlite3.connect,
    ) -> None:
        self.path = path
        self.policy = policy or RetryPolicy()
        self._connector = connector

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection]:
        conn = self._connector(
            str(self.path), timeout=self.policy.timeout_seconds, isolation_level=None
        )
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            # Cada evento confirmado precisa sobreviver a queda de energia, não só do processo.
            conn.execute("PRAGMA synchronous=FULL")
            yield conn
        finally:
            conn.close()

    def run[T](self, operation: Callable[[sqlite3.Connection], T], *, write: bool) -> T:
        """Executa a operação em uma transação, repetindo apenas falhas de lock."""
        attempts = self.policy.max_attempts
        for attempt in range(attempts):
            try:
                with self.connection() as conn:
                    return _in_transaction(conn, operation, write=write)
            except sqlite3.OperationalError as exc:
                if not is_lock_error(exc):
                    raise
                if attempt == attempts - 1:
                    raise HistoryUnavailableError() from exc
                self.policy.sleep(self.policy.delays[attempt])
        raise HistoryUnavailableError()

    def write[T](self, operation: Callable[[sqlite3.Connection], T]) -> T:
        return self.run(operation, write=True)

    def read[T](self, operation: Callable[[sqlite3.Connection], T]) -> T:
        return self.run(operation, write=False)


def _in_transaction[T](
    conn: sqlite3.Connection, operation: Callable[[sqlite3.Connection], T], *, write: bool
) -> T:
    # IMMEDIATE reserva a escrita no início, então a sequência lida dentro da transação é final.
    conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        result = operation(conn)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return result
