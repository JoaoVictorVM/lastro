"""Persistência da avaliação: transações curtas, lease com geração e registros append-only.

Nenhum lock SQLite é mantido durante processo externo; cada método é uma transação própria.
"""

import json
import secrets
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from lastro.history.database import Database, db_timestamp
from lastro.history.errors import HistoryError

LEASE_SECONDS = 30
HEARTBEAT_SECONDS = 5

type Clock = Callable[[], datetime]
type Entropy = Callable[[int], str]


class EvalStoreError(HistoryError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _load(value: str | None) -> Any:
    return json.loads(value) if value is not None else None


@dataclass(frozen=True)
class SuiteRecord:
    id: str
    suite_name: str
    suite_sha256: str
    manifest: dict[str, Any]
    catalog_sha256: str
    rubric_sha256: str
    executor_name: str
    executor_version: str
    model_identity: dict[str, Any]
    seed: int
    status: str
    pause_reason: str | None
    created_at: str
    updated_at: str
    ended_at: str | None
    lease_token: str | None
    lease_generation: int
    lease_expires_at: str | None
    owner: dict[str, Any] | None
    current_revision: int


@dataclass(frozen=True)
class TrialRecord:
    id: str
    suite_id: str
    ordinal: int
    condition: str
    scenario: str
    variant: str
    repetition: int
    role: str
    parent_trial_id: str | None
    status: str
    condition_sha256: str
    memory_sha256: str | None
    selected_attempt_id: str | None


@dataclass(frozen=True)
class AttemptRecord:
    id: str
    trial_id: str
    attempt: int
    status: str
    reason: str | None
    run_id: str | None
    history_relative_path: str
    executor_identity: dict[str, Any]
    started_at: str | None
    ended_at: str | None
    reset: dict[str, Any] | None
    version: dict[str, Any] | None
    snapshot_relative_path: str | None
    snapshot_sha256: str | None
    access_start: int | None
    access_end: int | None
    access_sha256: str | None
    usage: dict[str, Any] | None
    observed_cost_nusd: int | None
    logs_relative_path: str | None


@dataclass(frozen=True)
class JudgmentRecord:
    id: str
    attempt_id: str
    finding_ref: str
    input_sha256: str
    judge_identity: dict[str, Any]
    rubric_sha256: str
    status: str
    parse_attempts: int
    decision: str | None
    bug_id: str | None
    reason: str | None
    output: dict[str, Any] | None
    usage: dict[str, Any] | None
    observed_cost_nusd: int | None


@dataclass(frozen=True)
class ReviewItemRecord:
    id: str
    suite_id: str
    judgment_id: str
    manifest_id: str
    input_sha256: str
    sampled: bool
    selection_seed: int


@dataclass(frozen=True)
class ReviewRecord:
    id: str
    review_item_id: str
    revision: int
    previous_review_id: str | None
    reviewer: str
    decision: str
    bug_id: str | None
    reason: str
    payload_sha256: str


@dataclass(frozen=True)
class MeasurementInput:
    metric: str
    unit: str
    value: float | None
    numerator: float | None = None
    denominator: float | None = None
    unavailable_reason: str | None = None
    complete: bool = False
    source_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class MeasurementRecord:
    attempt_id: str
    revision: int
    metric: str
    unit: str
    value: float | None
    numerator: float | None
    denominator: float | None
    unavailable_reason: str | None
    complete: bool
    source_refs: tuple[str, ...]


@dataclass(frozen=True)
class ResultInput:
    stratum: dict[str, Any]
    stratum_key: str
    metric: str
    unit: str
    n: int
    planned_n: int
    invalid_n: int
    excluded_n: int
    mean: float | None
    sample_stddev: float | None
    complete: bool
    limitations: tuple[str, ...]
    inputs_sha256: str


@dataclass(frozen=True)
class ResultRecord:
    suite_id: str
    revision: int
    stratum: dict[str, Any]
    stratum_key: str
    metric: str
    unit: str
    n: int
    planned_n: int
    invalid_n: int
    excluded_n: int
    mean: float | None
    sample_stddev: float | None
    complete: bool
    limitations: tuple[str, ...]
    inputs_sha256: str


@dataclass(frozen=True)
class BudgetEntry:
    id: str
    suite_id: str
    attempt_id: str | None
    component: str
    request_key: str
    status: str
    reserved_nusd: int
    actual_nusd: int | None
    source_ref: str | None


@dataclass(frozen=True)
class EventRecord:
    sequence: int
    trial_id: str | None
    event_type: str
    payload: dict[str, Any]
    idempotency_key: str
    timestamp: str


@dataclass(frozen=True)
class NewTrial:
    ordinal: int
    condition: str
    scenario: str
    variant: str
    repetition: int
    role: str
    status: str
    condition_sha256: str
    memory_sha256: str | None
    parent_ordinal: int | None


@dataclass(frozen=True)
class NewSuite:
    suite_name: str
    suite_sha256: str
    manifest: dict[str, Any]
    catalog_sha256: str
    rubric_sha256: str
    executor_name: str
    executor_version: str
    model_identity: dict[str, Any]
    seed: int
    trials: Sequence[NewTrial]


@dataclass(frozen=True)
class Lease:
    suite_id: str
    token: str
    generation: int


@dataclass(frozen=True)
class RetrievalSampleRecord:
    """Pergunta e repetição avaliadas: contexto entregue, resposta, referência e métricas."""

    id: str
    suite_id: str
    trial_id: str
    attempt_id: str
    question_id: str
    repetition: int
    input_sha256: str
    snapshot: dict[str, Any]
    metrics: dict[str, Any]
    created_at: str


def _sample(row: sqlite3.Row) -> RetrievalSampleRecord:
    return RetrievalSampleRecord(
        id=row["id"],
        suite_id=row["suite_id"],
        trial_id=row["trial_id"],
        attempt_id=row["attempt_id"],
        question_id=row["question_id"],
        repetition=int(row["repetition"]),
        input_sha256=row["input_sha256"],
        snapshot=_load(row["snapshot_json"]) or {},
        metrics=_load(row["metrics_json"]) or {},
        created_at=row["created_at"],
    )


def _suite(row: sqlite3.Row) -> SuiteRecord:
    return SuiteRecord(
        id=row["id"],
        suite_name=row["suite_name"],
        suite_sha256=row["suite_sha256"],
        manifest=json.loads(row["manifest_json"]),
        catalog_sha256=row["catalog_sha256"],
        rubric_sha256=row["rubric_sha256"],
        executor_name=row["executor_name"],
        executor_version=row["executor_version"],
        model_identity=json.loads(row["model_identity_json"]),
        seed=int(row["seed"]),
        status=row["status"],
        pause_reason=row["pause_reason"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        ended_at=row["ended_at"],
        lease_token=row["lease_token"],
        lease_generation=int(row["lease_generation"]),
        lease_expires_at=row["lease_expires_at"],
        owner=_load(row["owner_json"]),
        current_revision=int(row["current_revision"]),
    )


def _trial(row: sqlite3.Row) -> TrialRecord:
    return TrialRecord(
        id=row["id"],
        suite_id=row["suite_id"],
        ordinal=int(row["ordinal"]),
        condition=row["condition"],
        scenario=row["scenario"],
        variant=row["variant"],
        repetition=int(row["repetition"]),
        role=row["role"],
        parent_trial_id=row["parent_trial_id"],
        status=row["status"],
        condition_sha256=row["condition_sha256"],
        memory_sha256=row["memory_sha256"],
        selected_attempt_id=row["selected_attempt_id"],
    )


def _attempt(row: sqlite3.Row) -> AttemptRecord:
    return AttemptRecord(
        id=row["id"],
        trial_id=row["trial_id"],
        attempt=int(row["attempt"]),
        status=row["status"],
        reason=row["reason"],
        run_id=row["run_id"],
        history_relative_path=row["history_relative_path"],
        executor_identity=json.loads(row["executor_identity_json"]),
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        reset=_load(row["reset_json"]),
        version=_load(row["version_json"]),
        snapshot_relative_path=row["snapshot_relative_path"],
        snapshot_sha256=row["snapshot_sha256"],
        access_start=row["access_start"],
        access_end=row["access_end"],
        access_sha256=row["access_sha256"],
        usage=_load(row["usage_json"]),
        observed_cost_nusd=row["observed_cost_nusd"],
        logs_relative_path=row["logs_relative_path"],
    )


def _judgment(row: sqlite3.Row) -> JudgmentRecord:
    return JudgmentRecord(
        id=row["id"],
        attempt_id=row["attempt_id"],
        finding_ref=row["finding_ref"],
        input_sha256=row["input_sha256"],
        judge_identity=json.loads(row["judge_identity_json"]),
        rubric_sha256=row["rubric_sha256"],
        status=row["status"],
        parse_attempts=int(row["parse_attempts"]),
        decision=row["decision"],
        bug_id=row["bug_id"],
        reason=row["reason"],
        output=_load(row["output_json"]),
        usage=_load(row["usage_json"]),
        observed_cost_nusd=row["observed_cost_nusd"],
    )


def _review(row: sqlite3.Row) -> ReviewRecord:
    return ReviewRecord(
        id=row["id"],
        review_item_id=row["review_item_id"],
        revision=int(row["revision"]),
        previous_review_id=row["previous_review_id"],
        reviewer=row["reviewer"],
        decision=row["decision"],
        bug_id=row["bug_id"],
        reason=row["reason"],
        payload_sha256=row["payload_sha256"],
    )


def _budget(row: sqlite3.Row) -> BudgetEntry:
    return BudgetEntry(
        id=row["id"],
        suite_id=row["suite_id"],
        attempt_id=row["attempt_id"],
        component=row["component"],
        request_key=row["request_key"],
        status=row["status"],
        reserved_nusd=int(row["reserved_nusd"]),
        actual_nusd=row["actual_nusd"],
        source_ref=row["source_ref"],
    )


class EvalRepository:
    """Porta de persistência da avaliação exposta pelo HistoryStore."""

    def __init__(
        self, database: Database, clock: Clock, entropy: Entropy = secrets.token_hex
    ) -> None:
        self._db = database
        self._clock = clock
        self._entropy = entropy

    def _now(self) -> str:
        return db_timestamp(self._clock())

    def _id(self, prefix: str) -> str:
        return f"{prefix}_{self._entropy(12)}"

    # Suites -----------------------------------------------------------------------------------

    def create_suite(self, data: NewSuite) -> SuiteRecord:
        suite_id = self._id("eval")
        now = self._now()

        def write(conn: sqlite3.Connection) -> SuiteRecord:
            conn.execute(
                "INSERT INTO eval_suites (id, suite_name, schema_version, suite_sha256, "
                "manifest_json, catalog_sha256, rubric_sha256, executor_name, executor_version, "
                "model_identity_json, seed, status, created_at, updated_at) "
                "VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    suite_id,
                    data.suite_name,
                    data.suite_sha256,
                    _json(data.manifest),
                    data.catalog_sha256,
                    data.rubric_sha256,
                    data.executor_name,
                    data.executor_version,
                    _json(data.model_identity),
                    data.seed,
                    now,
                    now,
                ),
            )
            ids: dict[int, str] = {}
            for trial in sorted(data.trials, key=lambda item: item.ordinal):
                trial_id = self._id("trial")
                ids[trial.ordinal] = trial_id
                parent = ids.get(trial.parent_ordinal) if trial.parent_ordinal else None
                if trial.parent_ordinal is not None and parent is None:
                    raise EvalStoreError("invalid_suite", "warmup must precede its paired trial")
                conn.execute(
                    "INSERT INTO eval_trials (id, suite_id, ordinal, condition, scenario, variant, "
                    "repetition, role, parent_trial_id, status, condition_sha256, memory_sha256, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        trial_id,
                        suite_id,
                        trial.ordinal,
                        trial.condition,
                        trial.scenario,
                        trial.variant,
                        trial.repetition,
                        trial.role,
                        parent,
                        trial.status,
                        trial.condition_sha256,
                        trial.memory_sha256,
                        now,
                    ),
                )
            _append_event(conn, suite_id, None, "suite_created", {}, f"created:{suite_id}", now)
            return _select_suite(conn, suite_id)

        return self._db.write(write)

    def get_suite(self, suite_id: str) -> SuiteRecord:
        return self._db.read(lambda conn: _select_suite(conn, suite_id))

    def list_suites(self) -> list[SuiteRecord]:
        rows = self._db.read(
            lambda conn: conn.execute("SELECT * FROM eval_suites ORDER BY created_at").fetchall()
        )
        return [_suite(row) for row in rows]

    def set_suite_status(
        self, lease: Lease | None, suite_id: str, status: str, reason: str | None = None
    ) -> SuiteRecord:
        now = self._now()
        ended = now if status in {"completed", "cancelled"} else None

        def write(conn: sqlite3.Connection) -> SuiteRecord:
            if lease is not None:
                _check_lease(conn, lease, now)
            conn.execute(
                "UPDATE eval_suites SET status = ?, pause_reason = ?, updated_at = ?, "
                "ended_at = COALESCE(?, ended_at) WHERE id = ?",
                (status, reason, now, ended, suite_id),
            )
            _append_event(
                conn,
                suite_id,
                None,
                "suite_status",
                {"status": status, "reason": reason},
                f"status:{status}:{self._entropy(6)}",
                now,
            )
            return _select_suite(conn, suite_id)

        return self._db.write(write)

    # Lease -------------------------------------------------------------------------------------

    def acquire_lease(
        self, suite_id: str, owner: dict[str, Any], *, owner_gone: Callable[[dict[str, Any]], bool]
    ) -> Lease:
        """Lease vencido só é tomado depois de provar que o dono anterior terminou."""
        now_moment = self._clock()
        now = db_timestamp(now_moment)
        expires = db_timestamp(now_moment + timedelta(seconds=LEASE_SECONDS))
        token = self._entropy(16)

        def write(conn: sqlite3.Connection) -> Lease:
            suite = _select_suite(conn, suite_id)
            if suite.lease_token is not None:
                assert suite.lease_expires_at is not None
                if suite.lease_expires_at > now:
                    raise EvalStoreError("lease_busy", f"suite {suite_id} has an active runner")
                if suite.owner is not None and not owner_gone(suite.owner):
                    raise EvalStoreError(
                        "owner_unverified",
                        f"previous runner of {suite_id} may still be alive; recovery required",
                    )
            generation = suite.lease_generation + 1
            conn.execute(
                "UPDATE eval_suites SET lease_token = ?, lease_generation = ?, "
                "lease_expires_at = ?, owner_json = ?, updated_at = ? WHERE id = ?",
                (token, generation, expires, _json(owner), now, suite_id),
            )
            return Lease(suite_id, token, generation)

        return self._db.write(write)

    def heartbeat(self, lease: Lease) -> bool:
        now_moment = self._clock()
        now = db_timestamp(now_moment)
        expires = db_timestamp(now_moment + timedelta(seconds=LEASE_SECONDS))

        def write(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "UPDATE eval_suites SET lease_expires_at = ? WHERE id = ? AND lease_token = ? "
                "AND lease_generation = ? AND lease_expires_at > ?",
                (expires, lease.suite_id, lease.token, lease.generation, now),
            )
            return cursor.rowcount == 1

        return self._db.write(write)

    def owns(self, lease: Lease) -> bool:
        now = self._now()

        def read(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                "SELECT 1 FROM eval_suites WHERE id = ? AND lease_token = ? "
                "AND lease_generation = ? AND lease_expires_at > ?",
                (lease.suite_id, lease.token, lease.generation, now),
            ).fetchone()
            return row is not None

        return self._db.read(read)

    def release_lease(self, lease: Lease) -> None:
        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE eval_suites SET lease_token = NULL, lease_expires_at = NULL, "
                "owner_json = NULL WHERE id = ? AND lease_token = ? AND lease_generation = ?",
                (lease.suite_id, lease.token, lease.generation),
            )

        self._db.write(write)

    # Trials e tentativas ------------------------------------------------------------------------

    def list_trials(self, suite_id: str) -> list[TrialRecord]:
        rows = self._db.read(
            lambda conn: conn.execute(
                "SELECT * FROM eval_trials WHERE suite_id = ? ORDER BY ordinal", (suite_id,)
            ).fetchall()
        )
        return [_trial(row) for row in rows]

    def get_trial(self, trial_id: str) -> TrialRecord:
        def read(conn: sqlite3.Connection) -> TrialRecord:
            row = conn.execute("SELECT * FROM eval_trials WHERE id = ?", (trial_id,)).fetchone()
            if row is None:
                raise EvalStoreError("not_found", f"trial {trial_id}")
            return _trial(row)

        return self._db.read(read)

    def set_trial_status(self, lease: Lease, trial_id: str, status: str) -> TrialRecord:
        now = self._now()

        def write(conn: sqlite3.Connection) -> TrialRecord:
            _check_lease(conn, lease, now)
            conn.execute("UPDATE eval_trials SET status = ? WHERE id = ?", (status, trial_id))
            row = conn.execute("SELECT * FROM eval_trials WHERE id = ?", (trial_id,)).fetchone()
            _append_event(
                conn,
                lease.suite_id,
                trial_id,
                "trial_status",
                {"status": status},
                f"trial:{trial_id}:{status}:{self._entropy(6)}",
                now,
            )
            return _trial(row)

        return self._db.write(write)

    def start_attempt(
        self,
        lease: Lease,
        trial_id: str,
        history_relative_path: str,
        executor_identity: dict[str, Any],
    ) -> AttemptRecord:
        attempt_id = self._id("att")
        now = self._now()

        def write(conn: sqlite3.Connection) -> AttemptRecord:
            _check_lease(conn, lease, now)
            row = conn.execute(
                "SELECT COALESCE(MAX(attempt), 0) + 1 AS next FROM eval_attempts "
                "WHERE trial_id = ?",
                (trial_id,),
            ).fetchone()
            number = int(row["next"])
            path = history_relative_path.replace("{attempt}", str(number))
            conn.execute(
                "INSERT INTO eval_attempts (id, trial_id, attempt, status, "
                "history_relative_path, executor_identity_json, started_at) "
                "VALUES (?, ?, ?, 'running', ?, ?, ?)",
                (attempt_id, trial_id, number, path, _json(executor_identity), now),
            )
            conn.execute("UPDATE eval_trials SET status = 'preparing' WHERE id = ?", (trial_id,))
            _append_event(
                conn,
                lease.suite_id,
                trial_id,
                "attempt_started",
                {"attempt": number},
                f"attempt:{attempt_id}",
                now,
            )
            return _select_attempt(conn, attempt_id)

        return self._db.write(write)

    def update_attempt(self, lease: Lease, attempt_id: str, **fields: Any) -> AttemptRecord:
        allowed = {
            "run_id",
            "reset_json",
            "version_json",
            "access_start",
            "access_end",
            "access_sha256",
            "usage_json",
            "observed_cost_nusd",
            "logs_relative_path",
            "executor_identity_json",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"campos não atualizáveis: {sorted(unknown)}")
        now = self._now()
        values = {
            key: _json(value) if key.endswith("_json") and value is not None else value
            for key, value in fields.items()
        }

        def write(conn: sqlite3.Connection) -> AttemptRecord:
            _check_lease(conn, lease, now)
            current = _select_attempt(conn, attempt_id)
            if current.snapshot_sha256 is not None:
                raise EvalStoreError("snapshot_changed", "completed attempt is immutable")
            if values:
                assignments = ", ".join(f"{key} = ?" for key in values)
                conn.execute(
                    f"UPDATE eval_attempts SET {assignments} WHERE id = ?",
                    (*values.values(), attempt_id),
                )
            return _select_attempt(conn, attempt_id)

        return self._db.write(write)

    def finish_attempt(
        self,
        lease: Lease,
        attempt_id: str,
        *,
        status: str,
        reason: str | None,
        trial_status: str,
        snapshot_relative_path: str | None = None,
        snapshot_sha256: str | None = None,
        select: bool = False,
    ) -> AttemptRecord:
        """Estado da tentativa, do trial, seleção e evento correspondente numa só transação."""
        now = self._now()

        def write(conn: sqlite3.Connection) -> AttemptRecord:
            _check_lease(conn, lease, now)
            current = _select_attempt(conn, attempt_id)
            if current.snapshot_sha256 is not None and current.status == "valid":
                raise EvalStoreError("snapshot_changed", "completed attempt is immutable")
            conn.execute(
                "UPDATE eval_attempts SET status = ?, reason = ?, ended_at = ?, "
                "snapshot_relative_path = ?, snapshot_sha256 = ? WHERE id = ?",
                (status, reason, now, snapshot_relative_path, snapshot_sha256, attempt_id),
            )
            conn.execute(
                "UPDATE eval_trials SET status = ? WHERE id = ?", (trial_status, current.trial_id)
            )
            if select:
                conn.execute(
                    "UPDATE eval_trials SET selected_attempt_id = ? WHERE id = ?",
                    (attempt_id, current.trial_id),
                )
            _append_event(
                conn,
                lease.suite_id,
                current.trial_id,
                "attempt_finished",
                {"status": status, "reason": reason, "trial_status": trial_status},
                f"finished:{attempt_id}",
                now,
            )
            return _select_attempt(conn, attempt_id)

        return self._db.write(write)

    def select_attempt(self, trial_id: str, attempt_id: str) -> None:
        """Escolha explícita; o trigger recusa tentativa de outro trial."""
        self._db.write(
            lambda conn: conn.execute(
                "UPDATE eval_trials SET selected_attempt_id = ? WHERE id = ?",
                (attempt_id, trial_id),
            )
        )

    def list_attempts(self, trial_id: str) -> list[AttemptRecord]:
        rows = self._db.read(
            lambda conn: conn.execute(
                "SELECT * FROM eval_attempts WHERE trial_id = ? ORDER BY attempt", (trial_id,)
            ).fetchall()
        )
        return [_attempt(row) for row in rows]

    def get_attempt(self, attempt_id: str) -> AttemptRecord:
        return self._db.read(lambda conn: _select_attempt(conn, attempt_id))

    def interrupted_attempts(self, suite_id: str) -> list[AttemptRecord]:
        rows = self._db.read(
            lambda conn: conn.execute(
                "SELECT a.* FROM eval_attempts a JOIN eval_trials t ON t.id = a.trial_id "
                "WHERE t.suite_id = ? AND a.status = 'running' ORDER BY t.ordinal, a.attempt",
                (suite_id,),
            ).fetchall()
        )
        return [_attempt(row) for row in rows]

    # Eventos -----------------------------------------------------------------------------------

    def append_event(
        self,
        suite_id: str,
        event_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
        trial_id: str | None = None,
    ) -> EventRecord:
        now = self._now()
        return self._db.write(
            lambda conn: _append_event(
                conn, suite_id, trial_id, event_type, payload, idempotency_key, now
            )
        )

    def list_events(self, suite_id: str) -> list[EventRecord]:
        rows = self._db.read(
            lambda conn: conn.execute(
                "SELECT * FROM eval_events WHERE suite_id = ? ORDER BY sequence", (suite_id,)
            ).fetchall()
        )
        return [
            EventRecord(
                sequence=int(row["sequence"]),
                trial_id=row["trial_id"],
                event_type=row["event_type"],
                payload=json.loads(row["payload_json"]),
                idempotency_key=row["idempotency_key"],
                timestamp=row["timestamp"],
            )
            for row in rows
        ]

    # Julgamentos -------------------------------------------------------------------------------

    def ensure_judgment(
        self,
        attempt_id: str,
        finding_ref: str,
        input_sha256: str,
        judge_identity: dict[str, Any],
        rubric_sha256: str,
    ) -> JudgmentRecord:
        """Idempotente pela chave natural; o resume nunca duplica um julgamento."""
        now = self._now()
        judgment_id = self._id("jdg")

        def write(conn: sqlite3.Connection) -> JudgmentRecord:
            conn.execute(
                "INSERT OR IGNORE INTO eval_judgments (id, attempt_id, finding_ref, input_sha256, "
                "judge_identity_json, rubric_sha256, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                (
                    judgment_id,
                    attempt_id,
                    finding_ref,
                    input_sha256,
                    _json(judge_identity),
                    rubric_sha256,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM eval_judgments WHERE attempt_id = ? AND finding_ref = ? "
                "AND input_sha256 = ? AND rubric_sha256 = ?",
                (attempt_id, finding_ref, input_sha256, rubric_sha256),
            ).fetchone()
            return _judgment(row)

        return self._db.write(write)

    def record_judgment(
        self,
        judgment_id: str,
        *,
        status: str,
        parse_attempts: int,
        decision: str | None = None,
        bug_id: str | None = None,
        reason: str | None = None,
        output: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
        observed_cost_nusd: int | None = None,
        judge_identity: dict[str, Any] | None = None,
    ) -> JudgmentRecord:
        now = self._now()
        completed = now if status in {"judged", "manual_required"} else None

        def write(conn: sqlite3.Connection) -> JudgmentRecord:
            conn.execute(
                "UPDATE eval_judgments SET status = ?, parse_attempts = ?, decision = ?, "
                "bug_id = ?, reason = ?, output_json = ?, usage_json = ?, observed_cost_nusd = ?, "
                "judge_identity_json = COALESCE(?, judge_identity_json), completed_at = ? "
                "WHERE id = ?",
                (
                    status,
                    parse_attempts,
                    decision,
                    bug_id,
                    reason,
                    _json(output) if output is not None else None,
                    _json(usage) if usage is not None else None,
                    observed_cost_nusd,
                    _json(judge_identity) if judge_identity is not None else None,
                    completed,
                    judgment_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM eval_judgments WHERE id = ?", (judgment_id,)
            ).fetchone()
            return _judgment(row)

        return self._db.write(write)

    def list_judgments(self, suite_id: str) -> list[JudgmentRecord]:
        rows = self._db.read(
            lambda conn: conn.execute(
                "SELECT j.* FROM eval_judgments j JOIN eval_attempts a ON a.id = j.attempt_id "
                "JOIN eval_trials t ON t.id = a.trial_id WHERE t.suite_id = ? "
                "ORDER BY t.ordinal, j.finding_ref",
                (suite_id,),
            ).fetchall()
        )
        return [_judgment(row) for row in rows]

    # Revisão humana ----------------------------------------------------------------------------

    def freeze_review_items(
        self, suite_id: str, manifest_id: str, items: Sequence[tuple[str, str, bool]], seed: int
    ) -> list[ReviewItemRecord]:
        """Amostra persistida antes da revisão; repetir o mesmo manifesto não a refaz."""
        now = self._now()

        def write(conn: sqlite3.Connection) -> list[ReviewItemRecord]:
            existing = conn.execute(
                "SELECT COUNT(*) AS n FROM eval_review_items "
                "WHERE suite_id = ? AND manifest_id = ?",
                (suite_id, manifest_id),
            ).fetchone()
            if int(existing["n"]) == 0:
                for judgment_id, input_sha256, sampled in items:
                    conn.execute(
                        "INSERT INTO eval_review_items (id, suite_id, judgment_id, manifest_id, "
                        "input_sha256, sampled, selection_seed, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            self._id("rvi"),
                            suite_id,
                            judgment_id,
                            manifest_id,
                            input_sha256,
                            int(sampled),
                            seed,
                            now,
                        ),
                    )
            return _review_items(conn, suite_id, manifest_id)

        return self._db.write(write)

    def review_items(self, suite_id: str) -> list[ReviewItemRecord]:
        def read(conn: sqlite3.Connection) -> list[ReviewItemRecord]:
            row = conn.execute(
                "SELECT manifest_id FROM eval_review_items WHERE suite_id = ? "
                "ORDER BY created_at LIMIT 1",
                (suite_id,),
            ).fetchone()
            return _review_items(conn, suite_id, row["manifest_id"]) if row else []

        return self._db.read(read)

    def import_reviews(
        self, entries: Sequence[tuple[str, str, str, str | None, str, str]]
    ) -> tuple[int, int]:
        """Lote atômico de (item, revisor, decisão, bug, motivo, hash do payload).

        Replay idêntico é ignorado; mudança cria nova revisão encadeada à anterior.
        """
        now = self._now()

        def write(conn: sqlite3.Connection) -> tuple[int, int]:
            created = 0
            replayed = 0
            for item_id, reviewer, decision, bug_id, reason, payload_sha256 in entries:
                same = conn.execute(
                    "SELECT 1 FROM eval_reviews WHERE review_item_id = ? AND payload_sha256 = ?",
                    (item_id, payload_sha256),
                ).fetchone()
                if same is not None:
                    replayed += 1
                    continue
                last = conn.execute(
                    "SELECT id, revision FROM eval_reviews WHERE review_item_id = ? "
                    "ORDER BY revision DESC LIMIT 1",
                    (item_id,),
                ).fetchone()
                revision = int(last["revision"]) + 1 if last else 1
                conn.execute(
                    "INSERT INTO eval_reviews (id, review_item_id, revision, previous_review_id, "
                    "reviewer, decision, bug_id, reason, payload_sha256, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._id("rev"),
                        item_id,
                        revision,
                        last["id"] if last else None,
                        reviewer,
                        decision,
                        bug_id,
                        reason,
                        payload_sha256,
                        now,
                    ),
                )
                created += 1
            return created, replayed

        return self._db.write(write)

    def latest_reviews(self, suite_id: str) -> dict[str, ReviewRecord]:
        rows = self._db.read(
            lambda conn: conn.execute(
                "SELECT r.* FROM eval_reviews r "
                "JOIN eval_review_items i ON i.id = r.review_item_id "
                "WHERE i.suite_id = ? ORDER BY r.revision",
                (suite_id,),
            ).fetchall()
        )
        latest: dict[str, ReviewRecord] = {}
        for row in rows:
            latest[row["review_item_id"]] = _review(row)
        return latest

    def all_reviews(self, suite_id: str) -> list[ReviewRecord]:
        rows = self._db.read(
            lambda conn: conn.execute(
                "SELECT r.* FROM eval_reviews r "
                "JOIN eval_review_items i ON i.id = r.review_item_id "
                "WHERE i.suite_id = ? ORDER BY r.review_item_id, r.revision",
                (suite_id,),
            ).fetchall()
        )
        return [_review(row) for row in rows]

    # Medidas e agregados ----------------------------------------------------------------------

    def save_revision(
        self,
        suite_id: str,
        measurements: Sequence[tuple[str, MeasurementInput]],
        results: Sequence[ResultInput],
    ) -> int:
        """Grava o conjunto completo e só então avança `current_revision`."""
        now = self._now()

        def write(conn: sqlite3.Connection) -> int:
            suite = _select_suite(conn, suite_id)
            revision = suite.current_revision + 1
            for attempt_id, item in measurements:
                conn.execute(
                    "INSERT INTO eval_measurements (id, attempt_id, revision, metric, unit, value, "
                    "numerator, denominator, unavailable_reason, complete, source_refs_json, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._id("msr"),
                        attempt_id,
                        revision,
                        item.metric,
                        item.unit,
                        item.value,
                        item.numerator,
                        item.denominator,
                        item.unavailable_reason,
                        int(item.complete),
                        _json(list(item.source_refs)),
                        now,
                    ),
                )
            for result in results:
                conn.execute(
                    "INSERT INTO eval_results (id, suite_id, condition, executor_name, "
                    "executor_version, revision, stratum_key, stratum_json, metric, unit, n, "
                    "planned_n, invalid_n, excluded_n, mean, sample_stddev, complete, "
                    "limitations_json, inputs_sha256, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._id("res"),
                        suite_id,
                        str(result.stratum.get("condition") or "unknown"),
                        str(result.stratum.get("executor") or suite.executor_name),
                        result.stratum.get("executor_version") or suite.executor_version,
                        revision,
                        result.stratum_key,
                        _json(result.stratum),
                        result.metric,
                        result.unit,
                        result.n,
                        result.planned_n,
                        result.invalid_n,
                        result.excluded_n,
                        result.mean,
                        result.sample_stddev,
                        int(result.complete),
                        _json(list(result.limitations)),
                        result.inputs_sha256,
                        now,
                    ),
                )
            conn.execute(
                "UPDATE eval_suites SET current_revision = ?, updated_at = ? WHERE id = ?",
                (revision, now, suite_id),
            )
            _append_event(
                conn,
                suite_id,
                None,
                "results_revision",
                {"revision": revision, "results": len(results)},
                f"revision:{revision}",
                now,
            )
            return revision

        return self._db.write(write)

    def list_results(self, suite_id: str, revision: int | None = None) -> list[ResultRecord]:
        def read(conn: sqlite3.Connection) -> list[ResultRecord]:
            target = revision
            if target is None:
                target = _select_suite(conn, suite_id).current_revision
            rows = conn.execute(
                "SELECT * FROM eval_results WHERE suite_id = ? AND revision = ? "
                "ORDER BY stratum_key, metric",
                (suite_id, target),
            ).fetchall()
            return [
                ResultRecord(
                    suite_id=row["suite_id"],
                    revision=int(row["revision"]),
                    stratum=json.loads(row["stratum_json"]),
                    stratum_key=row["stratum_key"],
                    metric=row["metric"],
                    unit=row["unit"],
                    n=int(row["n"]),
                    planned_n=int(row["planned_n"]),
                    invalid_n=int(row["invalid_n"]),
                    excluded_n=int(row["excluded_n"]),
                    mean=row["mean"],
                    sample_stddev=row["sample_stddev"],
                    complete=bool(row["complete"]),
                    limitations=tuple(json.loads(row["limitations_json"])),
                    inputs_sha256=row["inputs_sha256"],
                )
                for row in rows
            ]

        return self._db.read(read)

    def list_measurements(self, suite_id: str, revision: int) -> list[MeasurementRecord]:
        rows = self._db.read(
            lambda conn: conn.execute(
                "SELECT m.* FROM eval_measurements m JOIN eval_attempts a ON a.id = m.attempt_id "
                "JOIN eval_trials t ON t.id = a.trial_id WHERE t.suite_id = ? AND m.revision = ? "
                "ORDER BY t.ordinal, m.metric",
                (suite_id, revision),
            ).fetchall()
        )
        return [
            MeasurementRecord(
                attempt_id=row["attempt_id"],
                revision=int(row["revision"]),
                metric=row["metric"],
                unit=row["unit"],
                value=row["value"],
                numerator=row["numerator"],
                denominator=row["denominator"],
                unavailable_reason=row["unavailable_reason"],
                complete=bool(row["complete"]),
                source_refs=tuple(json.loads(row["source_refs_json"])),
            )
            for row in rows
        ]

    # Orçamento ---------------------------------------------------------------------------------

    def reserve_budget(
        self,
        suite_id: str,
        component: str,
        request_key: str,
        reserved_nusd: int,
        cap_nusd: int,
        attempt_id: str | None = None,
    ) -> BudgetEntry | None:
        """Reserva antes da chamada paga; None quando o teto da suite seria excedido."""
        now = self._now()

        def write(conn: sqlite3.Connection) -> BudgetEntry | None:
            existing = conn.execute(
                "SELECT * FROM eval_budget_entries WHERE suite_id = ? AND request_key = ?",
                (suite_id, request_key),
            ).fetchone()
            if existing is not None:
                return _budget(existing)
            if _committed(conn, suite_id) + reserved_nusd > cap_nusd:
                return None
            entry_id = self._id("bud")
            conn.execute(
                "INSERT INTO eval_budget_entries (id, suite_id, attempt_id, component, "
                "request_key, status, reserved_nusd, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?, ?)",
                (entry_id, suite_id, attempt_id, component, request_key, reserved_nusd, now, now),
            )
            row = conn.execute(
                "SELECT * FROM eval_budget_entries WHERE id = ?", (entry_id,)
            ).fetchone()
            return _budget(row)

        return self._db.write(write)

    def update_budget(
        self,
        suite_id: str,
        request_key: str,
        status: str,
        actual_nusd: int | None = None,
        source_ref: str | None = None,
    ) -> BudgetEntry:
        now = self._now()

        def write(conn: sqlite3.Connection) -> BudgetEntry:
            row = conn.execute(
                "SELECT * FROM eval_budget_entries WHERE suite_id = ? AND request_key = ?",
                (suite_id, request_key),
            ).fetchone()
            if row is None:
                raise EvalStoreError("not_found", f"budget entry {request_key}")
            current = _budget(row)
            if status == "released" and current.status in {"sent", "uncertain"}:
                raise EvalStoreError(
                    "budget_release_refused", "sent or uncertain charges cannot be released"
                )
            if current.status in {"settled", "released"}:
                return current
            conn.execute(
                "UPDATE eval_budget_entries SET status = ?, actual_nusd = COALESCE(?, actual_nusd),"
                " source_ref = COALESCE(?, source_ref), updated_at = ? WHERE id = ?",
                (status, actual_nusd, source_ref, now, current.id),
            )
            return _budget(
                conn.execute(
                    "SELECT * FROM eval_budget_entries WHERE id = ?", (current.id,)
                ).fetchone()
            )

        return self._db.write(write)

    def list_budget(self, suite_id: str) -> list[BudgetEntry]:
        rows = self._db.read(
            lambda conn: conn.execute(
                "SELECT * FROM eval_budget_entries WHERE suite_id = ? ORDER BY created_at",
                (suite_id,),
            ).fetchall()
        )
        return [_budget(row) for row in rows]

    def record_sample(
        self,
        lease: Lease,
        *,
        trial_id: str,
        attempt_id: str,
        question_id: str,
        repetition: int,
        input_sha256: str,
        snapshot: dict[str, Any],
        metrics: dict[str, Any],
    ) -> RetrievalSampleRecord:
        """Amostra gravada sob o lease da suite; repetir a mesma tentativa não duplica."""
        now = self._now()
        sample_id = self._id("rsm")

        def write(conn: sqlite3.Connection) -> RetrievalSampleRecord:
            _check_lease(conn, lease, now)
            conn.execute(
                "INSERT OR IGNORE INTO retrieval_samples (id, suite_id, trial_id, attempt_id, "
                "question_id, repetition, input_sha256, snapshot_json, metrics_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sample_id,
                    lease.suite_id,
                    trial_id,
                    attempt_id,
                    question_id,
                    repetition,
                    input_sha256,
                    _json(snapshot),
                    _json(metrics),
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM retrieval_samples WHERE attempt_id = ? AND question_id = ? "
                "AND repetition = ?",
                (attempt_id, question_id, repetition),
            ).fetchone()
            return _sample(row)

        return self._db.write(write)

    def list_samples(self, suite_id: str) -> list[RetrievalSampleRecord]:
        rows = self._db.read(
            lambda conn: conn.execute(
                "SELECT * FROM retrieval_samples WHERE suite_id = ? ORDER BY question_id, "
                "repetition, created_at",
                (suite_id,),
            ).fetchall()
        )
        return [_sample(row) for row in rows]

    def committed_nusd(self, suite_id: str) -> int:
        return self._db.read(lambda conn: _committed(conn, suite_id))


def _committed(conn: sqlite3.Connection, suite_id: str) -> int:
    """Reservado ainda aberto conta pelo reservado; liquidado, pelo valor real."""
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE "
        "WHEN status = 'settled' THEN COALESCE(actual_nusd, reserved_nusd) "
        "WHEN status = 'released' THEN 0 "
        "ELSE MAX(reserved_nusd, COALESCE(actual_nusd, 0)) END), 0) AS total "
        "FROM eval_budget_entries WHERE suite_id = ?",
        (suite_id,),
    ).fetchone()
    return int(row["total"])


def _select_suite(conn: sqlite3.Connection, suite_id: str) -> SuiteRecord:
    row = conn.execute("SELECT * FROM eval_suites WHERE id = ?", (suite_id,)).fetchone()
    if row is None:
        raise EvalStoreError("not_found", f"suite run {suite_id} not found")
    return _suite(row)


def _select_attempt(conn: sqlite3.Connection, attempt_id: str) -> AttemptRecord:
    row = conn.execute("SELECT * FROM eval_attempts WHERE id = ?", (attempt_id,)).fetchone()
    if row is None:
        raise EvalStoreError("not_found", f"attempt {attempt_id}")
    return _attempt(row)


def _review_items(
    conn: sqlite3.Connection, suite_id: str, manifest_id: str
) -> list[ReviewItemRecord]:
    rows = conn.execute(
        "SELECT * FROM eval_review_items WHERE suite_id = ? AND manifest_id = ? ORDER BY id",
        (suite_id, manifest_id),
    ).fetchall()
    return [
        ReviewItemRecord(
            id=row["id"],
            suite_id=row["suite_id"],
            judgment_id=row["judgment_id"],
            manifest_id=row["manifest_id"],
            input_sha256=row["input_sha256"],
            sampled=bool(row["sampled"]),
            selection_seed=int(row["selection_seed"]),
        )
        for row in rows
    ]


def _check_lease(conn: sqlite3.Connection, lease: Lease, now: str) -> None:
    row = conn.execute(
        "SELECT lease_token, lease_generation, lease_expires_at FROM eval_suites WHERE id = ?",
        (lease.suite_id,),
    ).fetchone()
    if (
        row is None
        or row["lease_token"] != lease.token
        or int(row["lease_generation"]) != lease.generation
        or row["lease_expires_at"] is None
        or row["lease_expires_at"] <= now
    ):
        raise EvalStoreError("lease_lost", "this runner no longer owns the suite")


def _append_event(
    conn: sqlite3.Connection,
    suite_id: str,
    trial_id: str | None,
    event_type: str,
    payload: dict[str, Any],
    idempotency_key: str,
    timestamp: str,
) -> EventRecord:
    existing = conn.execute(
        "SELECT * FROM eval_events WHERE suite_id = ? AND idempotency_key = ?",
        (suite_id, idempotency_key),
    ).fetchone()
    if existing is not None:
        return EventRecord(
            sequence=int(existing["sequence"]),
            trial_id=existing["trial_id"],
            event_type=existing["event_type"],
            payload=json.loads(existing["payload_json"]),
            idempotency_key=existing["idempotency_key"],
            timestamp=existing["timestamp"],
        )
    row = conn.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 AS next FROM eval_events WHERE suite_id = ?",
        (suite_id,),
    ).fetchone()
    sequence = int(row["next"])
    conn.execute(
        "INSERT INTO eval_events (suite_id, sequence, trial_id, event_type, payload_json, "
        "idempotency_key, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (suite_id, sequence, trial_id, event_type, _json(payload), idempotency_key, timestamp),
    )
    return EventRecord(sequence, trial_id, event_type, payload, idempotency_key, timestamp)
