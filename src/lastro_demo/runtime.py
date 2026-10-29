"""Estado vivo do servidor: flags ativas, sorteio da intermitência, geração de sessão e reset."""

import random
import time
from dataclasses import dataclass, field

from lastro_demo import db
from lastro_demo.config import DemoConfig
from lastro_demo.flags import FlagState
from lastro_demo.seed import load_seed


@dataclass(frozen=True)
class ResetOutcome:
    clinics: int
    employees: int
    flags: FlagState
    duration_ms: int


@dataclass
class DemoRuntime:
    config: DemoConfig
    flags: FlagState
    rng: random.Random = field(init=False)
    session_generation: int = 0

    def __post_init__(self) -> None:
        self.rng = random.Random(self.config.random_seed)

    def initialize(self) -> ResetOutcome:
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        db.create_schema(self.config.db_path)
        return self.reset()

    def reset(self, flags: FlagState | None = None) -> ResetOutcome:
        """Recria os dados numa transação, reinicia o sorteio e invalida as sessões abertas."""
        started = time.perf_counter()
        new_flags = flags if flags is not None else self.flags
        with db.connection(self.config.db_path) as conn:
            with db.transaction(conn):
                load_seed(conn, self.config.users, new_flags)
            clinics = int(conn.execute("SELECT COUNT(*) FROM clinics").fetchone()[0])
            employees = int(conn.execute("SELECT COUNT(*) FROM employees").fetchone()[0])
        self.flags = new_flags
        self.rng = random.Random(self.config.random_seed)
        self.session_generation += 1
        duration_ms = round((time.perf_counter() - started) * 1000)
        return ResetOutcome(clinics, employees, new_flags, duration_ms)
