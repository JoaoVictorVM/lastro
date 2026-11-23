"""Persistência do modo autônomo: agendas, fila com lease, comparação e outbox.

Todo SQL é parametrizado e cada operação roda numa transação curta. Escritas de estado do item
exigem o token e a geração atuais do lease do perfil; perder o lease impede novos efeitos.
"""

import json
import sqlite3
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any, cast

from lastro.autonomous.models import (
    LEASE_LOST,
    MAX_NOTIFICATION_ATTEMPTS,
    NOTIFICATION_SCHEDULE,
    QUEUE_CAPACITY,
    QUEUE_UNAVAILABLE,
    SCHEDULE_CONFLICT,
    TRIGGER_CONFLICT,
    AttemptStatus,
    AutonomousError,
    ComparisonResult,
    ComparisonStatus,
    DeliveryResult,
    DeliveryStatus,
    EnqueueResult,
    Lease,
    MatchLabel,
    MatchRecord,
    MissionSnapshot,
    NotificationMessage,
    NotificationRecord,
    Occurrence,
    OccurrenceStatus,
    QueueItem,
    QueueStatus,
    ScheduleDefinition,
    ScheduleState,
    StoredComparison,
    TriggerInput,
    TriggerKind,
    canonical,
    new_id,
)
from lastro.history.database import Database, db_timestamp
from lastro.history.errors import HistoryError
from lastro.history.events import insert_event, prepare_event
from lastro.history.models import AUTONOMOUS_EVENTS, EventRecord, EventType
from lastro.history.runs import select_run

type Clock = Callable[[], datetime]

QUEUE_CAPACITY_REASON = "queue_capacity"


def _loads(text: str | None) -> Any:
    return json.loads(text) if text else None


def _item(row: sqlite3.Row) -> QueueItem:
    return QueueItem(
        id=row["id"],
        project_id=row["project_id"],
        profile=row["profile_name"],
        schedule_id=row["schedule_id"],
        trigger_kind=TriggerKind(row["trigger_kind"]),
        trigger_key=row["trigger_key"],
        trigger_sha256=row["trigger_sha256"],
        mission=MissionSnapshot.from_json(_loads(row["mission_snapshot_json"])),
        change_context_ref=row["change_context_ref"],
        trigger_snapshot=_loads(row["trigger_snapshot_json"]) or {},
        status=QueueStatus(row["status"]),
        reason=row["reason"],
        queued_at=row["queued_at"],
        claimed_at=row["claimed_at"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        run_id=row["run_id"],
        report_snapshot_id=row["report_snapshot_id"],
    )


def _schedule(row: sqlite3.Row) -> ScheduleDefinition:
    return ScheduleDefinition(
        id=row["id"],
        project_id=row["project_id"],
        name=row["name"],
        profile=row["profile_name"],
        mission_path=row["mission_path"],
        daily_time=row["daily_time"],
        timezone=row["timezone"],
        executor=row["executor"],
        settings=_loads(row["settings_json"]) or {},
        fingerprint=row["fingerprint_sha256"],
        backend=row["backend"],
        external_id=row["external_id"],
        state=ScheduleState(row["state"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        removed_at=row["removed_at"],
    )


def _occurrence(row: sqlite3.Row) -> Occurrence:
    return Occurrence(
        id=row["id"],
        schedule_id=row["schedule_id"],
        local_date=row["local_date"],
        due_at=row["due_at"],
        observed_at=row["observed_at"],
        status=OccurrenceStatus(row["status"]),
        item_id=row["item_id"],
        reason=row["reason"],
    )


def _match(row: sqlite3.Row) -> MatchRecord:
    return MatchRecord(
        flow=row["flow"],
        label=MatchLabel(row["label"]),
        current_finding_id=row["current_finding_id"],
        previous_finding_id=row["previous_finding_id"],
        signature_sha256=row["signature_sha256"],
        score=row["score"],
        match_rule=row["match_rule"],
        detail=_loads(row["detail_json"]) or {},
    )


def _notification(row: sqlite3.Row) -> NotificationRecord:
    return NotificationRecord(
        id=row["id"],
        queue_item_id=row["queue_item_id"],
        channel_name=row["channel_name"],
        channel_type=row["channel_type"],
        message=NotificationMessage.from_json(_loads(row["message_json"])),
        message_sha256=row["message_sha256"],
        destination_label=row["destination_label"],
        status=DeliveryStatus(row["status"]),
        attempts=int(row["attempts"]),
        next_attempt_at=row["next_attempt_at"],
        delivered_at=row["delivered_at"],
        last_error_code=row["last_error_code"],
        created_at=row["created_at"],
    )


class AutonomousRepository:
    """Fachada transacional da automação; nenhuma conexão sai daqui."""

    def __init__(self, database: Database, clock: Clock, project_id: str) -> None:
        self._database = database
        self._clock = clock
        self.project_id = project_id

    def _now(self) -> str:
        return db_timestamp(self._clock())

    def _write[T](self, operation: Callable[[sqlite3.Connection], T]) -> T:
        try:
            return self._database.write(operation)
        except AutonomousError:
            raise
        except (sqlite3.Error, HistoryError) as exc:
            if isinstance(exc, sqlite3.IntegrityError):
                raise AutonomousError(QUEUE_UNAVAILABLE, f"integrity violation: {exc}") from exc
            raise AutonomousError(QUEUE_UNAVAILABLE, "history is unavailable") from exc

    def _read[T](self, operation: Callable[[sqlite3.Connection], T]) -> T:
        try:
            return self._database.read(operation)
        except (sqlite3.Error, HistoryError) as exc:
            raise AutonomousError(QUEUE_UNAVAILABLE, "history is unavailable") from exc

    # Fila ---------------------------------------------------------------------------------------

    def enqueue(self, trigger: TriggerInput) -> EnqueueResult:
        """Inserção, deduplicação e capacidade na mesma transação IMMEDIATE."""
        now = self._now()
        sha = trigger.fingerprint

        def write(conn: sqlite3.Connection) -> EnqueueResult:
            row = conn.execute(
                "SELECT * FROM autonomous_queue_items WHERE project_id = ? AND profile_name = ? "
                "AND trigger_kind = ? AND trigger_key = ?",
                (self.project_id, trigger.profile, trigger.kind.value, trigger.key),
            ).fetchone()
            if row is not None:
                existing = _item(row)
                if existing.trigger_sha256 != sha:
                    raise AutonomousError(
                        TRIGGER_CONFLICT,
                        f"trigger key {trigger.key} was already used with a different body",
                    )
                return EnqueueResult(existing, False, (), _position(conn, existing))
            item_id = new_id("aq")
            conn.execute(
                "INSERT INTO autonomous_queue_items (id, project_id, profile_name, schedule_id, "
                "trigger_kind, trigger_key, trigger_sha256, mission_snapshot_json, mission_sha256, "
                "trigger_snapshot_json, status, queued_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
                (
                    item_id,
                    self.project_id,
                    trigger.profile,
                    trigger.schedule_id,
                    trigger.kind.value,
                    trigger.key,
                    sha,
                    canonical(trigger.mission.as_json()),
                    trigger.mission.sha256,
                    canonical(trigger.snapshot()),
                    now,
                ),
            )
            pending = conn.execute(
                "SELECT id FROM autonomous_queue_items WHERE project_id = ? AND profile_name = ? "
                "AND status = 'queued' ORDER BY queued_at, rowid",
                (self.project_id, trigger.profile),
            ).fetchall()
            excess = [str(item["id"]) for item in pending][: max(0, len(pending) - QUEUE_CAPACITY)]
            dropped: list[QueueItem] = []
            for dropped_id in excess:
                conn.execute(
                    "UPDATE autonomous_queue_items SET status = 'dropped', reason = ?, "
                    "ended_at = ? WHERE id = ? AND status = 'queued'",
                    (QUEUE_CAPACITY_REASON, now, dropped_id),
                )
                dropped.append(_item(_select_item(conn, dropped_id)))
            created = _item(_select_item(conn, item_id))
            return EnqueueResult(created, True, tuple(dropped), _position(conn, created))

        return self._write(write)

    def get_item(self, item_id: str) -> QueueItem:
        return self._read(lambda conn: _item(_select_item(conn, item_id)))

    def item_for_run(self, run_id: str) -> QueueItem | None:
        def read(conn: sqlite3.Connection) -> QueueItem | None:
            row = conn.execute(
                "SELECT * FROM autonomous_queue_items WHERE run_id = ?", (run_id,)
            ).fetchone()
            return _item(row) if row is not None else None

        return self._read(read)

    def list_items(
        self, profile: str | None = None, statuses: Sequence[QueueStatus] | None = None
    ) -> list[QueueItem]:
        def read(conn: sqlite3.Connection) -> list[QueueItem]:
            sql = "SELECT * FROM autonomous_queue_items WHERE project_id = ?"
            params: list[Any] = [self.project_id]
            if profile is not None:
                sql += " AND profile_name = ?"
                params.append(profile)
            if statuses:
                sql += f" AND status IN ({', '.join('?' for _ in statuses)})"
                params.extend(status.value for status in statuses)
            sql += " ORDER BY queued_at, rowid"
            return [_item(row) for row in conn.execute(sql, params).fetchall()]

        return self._read(read)

    def profiles_with_work(self) -> list[str]:
        def read(conn: sqlite3.Connection) -> list[str]:
            rows = conn.execute(
                "SELECT DISTINCT profile_name FROM autonomous_queue_items WHERE project_id = ? "
                "AND status IN ('queued', 'claimed', 'running') ORDER BY profile_name",
                (self.project_id,),
            ).fetchall()
            return [str(row["profile_name"]) for row in rows]

        return self._read(read)

    # Lease --------------------------------------------------------------------------------------

    def acquire_lease(
        self, profile: str, owner: dict[str, Any], ttl_seconds: int, token: str
    ) -> Lease | None:
        """Lease livre ou expirado vira deste worker, com geração nova; ocupado devolve None."""
        moment = self._clock()
        now = db_timestamp(moment)
        expires = db_timestamp(moment + timedelta(seconds=ttl_seconds))

        def write(conn: sqlite3.Connection) -> Lease | None:
            row = conn.execute(
                "SELECT * FROM autonomous_profile_leases WHERE project_id = ? AND profile_name = ?",
                (self.project_id, profile),
            ).fetchone()
            if row is None:
                generation = 1
                conn.execute(
                    "INSERT INTO autonomous_profile_leases (project_id, profile_name, token, "
                    "generation, owner_json, expires_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (self.project_id, profile, token, generation, canonical(owner), expires, now),
                )
            else:
                if row["token"] is not None and str(row["expires_at"]) > now:
                    return None
                generation = int(row["generation"]) + 1
                conn.execute(
                    "UPDATE autonomous_profile_leases SET token = ?, generation = ?, "
                    "owner_json = ?, expires_at = ?, updated_at = ? "
                    "WHERE project_id = ? AND profile_name = ?",
                    (token, generation, canonical(owner), expires, now, self.project_id, profile),
                )
            return Lease(self.project_id, profile, token, generation, expires)

        return self._write(write)

    def renew_lease(self, lease: Lease, ttl_seconds: int) -> Lease:
        moment = self._clock()
        expires = db_timestamp(moment + timedelta(seconds=ttl_seconds))

        def write(conn: sqlite3.Connection) -> Lease:
            _require_lease(conn, lease, db_timestamp(moment))
            conn.execute(
                "UPDATE autonomous_profile_leases SET expires_at = ?, updated_at = ? "
                "WHERE project_id = ? AND profile_name = ? AND token = ? AND generation = ?",
                (
                    expires,
                    db_timestamp(moment),
                    lease.project_id,
                    lease.profile,
                    lease.token,
                    lease.generation,
                ),
            )
            return Lease(lease.project_id, lease.profile, lease.token, lease.generation, expires)

        return self._write(write)

    def release_lease(self, lease: Lease) -> None:
        now = self._now()
        self._write(
            lambda conn: conn.execute(
                "UPDATE autonomous_profile_leases SET token = NULL, owner_json = NULL, "
                "expires_at = NULL, updated_at = ? WHERE project_id = ? AND profile_name = ? "
                "AND token = ? AND generation = ?",
                (now, lease.project_id, lease.profile, lease.token, lease.generation),
            )
        )

    def check_lease(self, lease: Lease) -> None:
        now = self._now()
        self._read(lambda conn: _require_lease(conn, lease, now))

    def lease_owner(self, profile: str) -> dict[str, Any] | None:
        def read(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                "SELECT owner_json, expires_at FROM autonomous_profile_leases "
                "WHERE project_id = ? AND profile_name = ?",
                (self.project_id, profile),
            ).fetchone()
            if row is None or row["owner_json"] is None:
                return None
            owner = cast(dict[str, Any], _loads(row["owner_json"]))
            return {**owner, "expires_at": row["expires_at"]}

        return self._read(read)

    def claim_next(self, lease: Lease) -> QueueItem | None:
        """Item ativo deixado por um owner anterior volta primeiro, para reconciliação."""
        now = self._now()

        def write(conn: sqlite3.Connection) -> QueueItem | None:
            _require_lease(conn, lease, now)
            active = conn.execute(
                "SELECT * FROM autonomous_queue_items WHERE project_id = ? AND profile_name = ? "
                "AND status IN ('claimed', 'running') ORDER BY queued_at, rowid LIMIT 1",
                (lease.project_id, lease.profile),
            ).fetchone()
            if active is not None:
                return _item(active)
            row = conn.execute(
                "SELECT id FROM autonomous_queue_items WHERE project_id = ? AND profile_name = ? "
                "AND status = 'queued' ORDER BY queued_at, rowid LIMIT 1",
                (lease.project_id, lease.profile),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE autonomous_queue_items SET status = 'claimed', claimed_at = ? "
                "WHERE id = ? AND status = 'queued'",
                (now, row["id"]),
            )
            return _item(_select_item(conn, str(row["id"])))

        return self._write(write)

    def mark_running(self, lease: Lease, item_id: str) -> QueueItem:
        now = self._now()

        def write(conn: sqlite3.Connection) -> QueueItem:
            _require_lease(conn, lease, now)
            conn.execute(
                "UPDATE autonomous_queue_items SET status = 'running', "
                "started_at = COALESCE(started_at, ?) WHERE id = ? AND status IN "
                "('claimed', 'running')",
                (now, item_id),
            )
            return _item(_select_item(conn, item_id))

        return self._write(write)

    def link_run(self, lease: Lease, item_id: str, run_id: str) -> QueueItem:
        """Compare-and-swap de null para a run; vínculo existente precisa ser o mesmo."""
        now = self._now()

        def write(conn: sqlite3.Connection) -> QueueItem:
            _require_lease(conn, lease, now)
            current = _item(_select_item(conn, item_id))
            if current.run_id is not None and current.run_id != run_id:
                raise AutonomousError(QUEUE_UNAVAILABLE, "item is already linked to another run")
            conn.execute(
                "UPDATE autonomous_queue_items SET run_id = ? WHERE id = ? AND run_id IS NULL",
                (run_id, item_id),
            )
            return _item(_select_item(conn, item_id))

        return self._write(write)

    def set_change_context(self, lease: Lease, item_id: str, reference: str) -> None:
        now = self._now()

        def write(conn: sqlite3.Connection) -> None:
            _require_lease(conn, lease, now)
            conn.execute(
                "UPDATE autonomous_queue_items SET change_context_ref = ? WHERE id = ? "
                "AND change_context_ref IS NULL",
                (reference, item_id),
            )

        self._write(write)

    def finish_item(
        self,
        lease: Lease,
        item_id: str,
        status: QueueStatus,
        reason: str | None,
        report_snapshot_id: str | None = None,
    ) -> QueueItem:
        now = self._now()

        def write(conn: sqlite3.Connection) -> QueueItem:
            _require_lease(conn, lease, now)
            current = _item(_select_item(conn, item_id))
            if current.status.terminal:
                return current
            conn.execute(
                "UPDATE autonomous_queue_items SET status = ?, reason = ?, ended_at = ?, "
                "report_snapshot_id = COALESCE(?, report_snapshot_id) WHERE id = ?",
                (status.value, reason, now, report_snapshot_id, item_id),
            )
            return _item(_select_item(conn, item_id))

        return self._write(write)

    def set_report(self, item_id: str, report_snapshot_id: str) -> None:
        self._write(
            lambda conn: conn.execute(
                "UPDATE autonomous_queue_items SET report_snapshot_id = ? WHERE id = ?",
                (report_snapshot_id, item_id),
            )
        )

    def baseline_items(self, item: QueueItem) -> list[QueueItem]:
        """Itens anteriores da mesma missão congelada e perfil, mais recentes primeiro."""

        def read(conn: sqlite3.Connection) -> list[QueueItem]:
            rows = conn.execute(
                "SELECT * FROM autonomous_queue_items WHERE project_id = ? AND profile_name = ? "
                "AND mission_sha256 = ? AND run_id IS NOT NULL AND id <> ? "
                "AND (queued_at < ? OR (queued_at = ? AND rowid < "
                "(SELECT rowid FROM autonomous_queue_items WHERE id = ?))) "
                "ORDER BY queued_at DESC, rowid DESC",
                (
                    item.project_id,
                    item.profile,
                    item.mission.sha256,
                    item.id,
                    item.queued_at,
                    item.queued_at,
                    item.id,
                ),
            ).fetchall()
            return [_item(row) for row in rows]

        return self._read(read)

    # Agendas ------------------------------------------------------------------------------------

    def save_schedule(
        self, definition: ScheduleDefinition, *, replace: bool
    ) -> tuple[ScheduleDefinition, bool]:
        """Mesmo conteúdo é idempotente; conteúdo diferente exige replace explícito."""
        now = self._now()

        def write(conn: sqlite3.Connection) -> tuple[ScheduleDefinition, bool]:
            row = conn.execute(
                "SELECT * FROM autonomous_schedules WHERE project_id = ? AND name = ?",
                (self.project_id, definition.name),
            ).fetchone()
            if row is not None:
                existing = _schedule(row)
                same = existing.fingerprint == definition.fingerprint
                if existing.state != ScheduleState.REMOVED and same:
                    return existing, False
                if existing.state != ScheduleState.REMOVED and not replace:
                    raise AutonomousError(
                        SCHEDULE_CONFLICT,
                        f"schedule {definition.name} exists with different content; use --replace",
                    )
                conn.execute(
                    "UPDATE autonomous_schedules SET profile_name = ?, mission_path = ?, "
                    "daily_time = ?, timezone = ?, executor = ?, settings_json = ?, "
                    "fingerprint_sha256 = ?, backend = ?, external_id = ?, state = 'active', "
                    "updated_at = ?, removed_at = NULL WHERE id = ?",
                    (
                        definition.profile,
                        definition.mission_path,
                        definition.daily_time,
                        definition.timezone,
                        definition.executor,
                        canonical(dict(definition.settings)),
                        definition.fingerprint,
                        definition.backend,
                        definition.external_id,
                        now,
                        existing.id,
                    ),
                )
                return _schedule(_select_schedule(conn, existing.id)), True
            conn.execute(
                "INSERT INTO autonomous_schedules (id, project_id, name, profile_name, "
                "mission_path, daily_time, timezone, executor, settings_json, fingerprint_sha256, "
                "backend, external_id, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                (
                    definition.id,
                    self.project_id,
                    definition.name,
                    definition.profile,
                    definition.mission_path,
                    definition.daily_time,
                    definition.timezone,
                    definition.executor,
                    canonical(dict(definition.settings)),
                    definition.fingerprint,
                    definition.backend,
                    definition.external_id,
                    now,
                    now,
                ),
            )
            return _schedule(_select_schedule(conn, definition.id)), True

        return self._write(write)

    def find_schedule(self, reference: str) -> ScheduleDefinition | None:
        def read(conn: sqlite3.Connection) -> ScheduleDefinition | None:
            row = conn.execute(
                "SELECT * FROM autonomous_schedules WHERE project_id = ? AND (id = ? OR name = ?)",
                (self.project_id, reference, reference),
            ).fetchone()
            return _schedule(row) if row is not None else None

        return self._read(read)

    def list_schedules(
        self, profile: str | None = None, *, include_removed: bool = False
    ) -> list[ScheduleDefinition]:
        def read(conn: sqlite3.Connection) -> list[ScheduleDefinition]:
            sql = "SELECT * FROM autonomous_schedules WHERE project_id = ?"
            params: list[Any] = [self.project_id]
            if profile is not None:
                sql += " AND profile_name = ?"
                params.append(profile)
            if not include_removed:
                sql += " AND state <> 'removed'"
            sql += " ORDER BY name"
            return [_schedule(row) for row in conn.execute(sql, params).fetchall()]

        return self._read(read)

    def set_schedule_state(self, schedule_id: str, state: ScheduleState) -> ScheduleDefinition:
        now = self._now()

        def write(conn: sqlite3.Connection) -> ScheduleDefinition:
            conn.execute(
                "UPDATE autonomous_schedules SET state = ?, updated_at = ?, removed_at = ? "
                "WHERE id = ?",
                (state.value, now, now if state == ScheduleState.REMOVED else None, schedule_id),
            )
            return _schedule(_select_schedule(conn, schedule_id))

        return self._write(write)

    def get_occurrence(self, schedule_id: str, local_date: str) -> Occurrence | None:
        def read(conn: sqlite3.Connection) -> Occurrence | None:
            row = conn.execute(
                "SELECT * FROM autonomous_occurrences WHERE schedule_id = ? AND local_date = ?",
                (schedule_id, local_date),
            ).fetchone()
            return _occurrence(row) if row is not None else None

        return self._read(read)

    def record_occurrence(
        self,
        schedule_id: str,
        local_date: str,
        due_at: str,
        status: OccurrenceStatus,
        *,
        item_id: str | None = None,
        reason: str | None = None,
    ) -> tuple[Occurrence, bool]:
        """Uma ocorrência por data local; repetição devolve a existente."""
        now = self._now()

        def write(conn: sqlite3.Connection) -> tuple[Occurrence, bool]:
            row = conn.execute(
                "SELECT * FROM autonomous_occurrences WHERE schedule_id = ? AND local_date = ?",
                (schedule_id, local_date),
            ).fetchone()
            if row is not None:
                return _occurrence(row), False
            occurrence_id = new_id("aocc")
            conn.execute(
                "INSERT INTO autonomous_occurrences (id, schedule_id, local_date, due_at, "
                "observed_at, status, item_id, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    occurrence_id,
                    schedule_id,
                    local_date,
                    due_at,
                    now,
                    status.value,
                    item_id,
                    reason,
                ),
            )
            created = conn.execute(
                "SELECT * FROM autonomous_occurrences WHERE id = ?", (occurrence_id,)
            ).fetchone()
            return _occurrence(created), True

        return self._write(write)

    def last_occurrence(self, schedule_id: str) -> Occurrence | None:
        def read(conn: sqlite3.Connection) -> Occurrence | None:
            row = conn.execute(
                "SELECT * FROM autonomous_occurrences WHERE schedule_id = ? "
                "ORDER BY local_date DESC LIMIT 1",
                (schedule_id,),
            ).fetchone()
            return _occurrence(row) if row is not None else None

        return self._read(read)

    # Comparação ---------------------------------------------------------------------------------

    def save_comparison(self, item_id: str, result: ComparisonResult) -> StoredComparison:
        """Uma comparação por item; repetir devolve a já persistida, sem recalcular."""
        now = self._now()

        def write(conn: sqlite3.Connection) -> StoredComparison:
            existing = _select_comparison(conn, "queue_item_id", item_id)
            if existing is not None:
                return existing
            comparison_id = new_id("acmp")
            conn.execute(
                "INSERT INTO autonomous_comparisons (id, queue_item_id, run_id, baseline_run_id, "
                "status, algorithm, algorithm_sha256, coverage_json, counts_json, "
                "limitations_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    comparison_id,
                    item_id,
                    result.run_id,
                    result.baseline_run_id,
                    result.status.value,
                    result.algorithm,
                    result.algorithm_sha256,
                    canonical(dict(result.coverage)),
                    canonical(result.counts),
                    canonical(list(result.limitations)),
                    now,
                ),
            )
            for match in result.matches:
                conn.execute(
                    "INSERT INTO autonomous_finding_matches (id, comparison_id, flow, label, "
                    "current_finding_id, previous_finding_id, signature_sha256, score, "
                    "match_rule, detail_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        new_id("amat"),
                        comparison_id,
                        match.flow,
                        match.label.value,
                        match.current_finding_id,
                        match.previous_finding_id,
                        match.signature_sha256,
                        match.score,
                        match.match_rule,
                        canonical(dict(match.detail)),
                    ),
                )
            stored = _select_comparison(conn, "id", comparison_id)
            assert stored is not None
            return stored

        return self._write(write)

    def comparison_for_item(self, item_id: str) -> StoredComparison | None:
        return self._read(lambda conn: _select_comparison(conn, "queue_item_id", item_id))

    def comparison_for_run(self, run_id: str) -> StoredComparison | None:
        return self._read(lambda conn: _select_comparison(conn, "run_id", run_id))

    # Outbox -------------------------------------------------------------------------------------

    def create_notifications(
        self, item_id: str, channels: Sequence[tuple[str, str]], message: NotificationMessage
    ) -> list[NotificationRecord]:
        """Uma entrega por canal e mensagem; repetição após crash não duplica."""
        now = self._now()
        digest = message.sha256

        def write(conn: sqlite3.Connection) -> list[NotificationRecord]:
            for name, kind in channels:
                conn.execute(
                    "INSERT OR IGNORE INTO autonomous_notifications (id, queue_item_id, "
                    "channel_name, channel_type, message_json, message_sha256, "
                    "destination_label, status, attempts, next_attempt_at, created_at, "
                    "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)",
                    (
                        new_id("anot"),
                        item_id,
                        name,
                        kind,
                        canonical(message.as_json()),
                        digest,
                        name,
                        now,
                        now,
                        now,
                    ),
                )
            return _select_notifications(conn, item_id)

        return self._write(write)

    def list_notifications(self, item_id: str) -> list[NotificationRecord]:
        return self._read(lambda conn: _select_notifications(conn, item_id))

    def due_notifications(self, item_id: str | None = None) -> list[NotificationRecord]:
        now = self._now()

        def read(conn: sqlite3.Connection) -> list[NotificationRecord]:
            sql = (
                "SELECT n.* FROM autonomous_notifications n JOIN autonomous_queue_items q "
                "ON q.id = n.queue_item_id WHERE q.project_id = ? AND n.status = 'pending' "
                "AND n.next_attempt_at <= ?"
            )
            params: list[Any] = [self.project_id, now]
            if item_id is not None:
                sql += " AND n.queue_item_id = ?"
                params.append(item_id)
            sql += " ORDER BY n.next_attempt_at, n.id"
            return [_notification(row) for row in conn.execute(sql, params).fetchall()]

        return self._read(read)

    def record_attempt(
        self, notification_id: str, requested_at: str, result: DeliveryResult
    ) -> NotificationRecord:
        """Grava a tentativa e agenda a próxima no instante alvo; a terceira falha é final."""
        moment = self._clock()
        now = db_timestamp(moment)

        def write(conn: sqlite3.Connection) -> NotificationRecord:
            row = conn.execute(
                "SELECT * FROM autonomous_notifications WHERE id = ?", (notification_id,)
            ).fetchone()
            if row is None:
                raise AutonomousError(QUEUE_UNAVAILABLE, "notification not found")
            current = _notification(row)
            if current.status != DeliveryStatus.PENDING:
                return current
            ordinal = current.attempts + 1
            conn.execute(
                "INSERT INTO autonomous_notification_attempts (id, notification_id, ordinal, "
                "requested_at, completed_at, status, response_code, latency_ms, error_code) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id("anat"),
                    notification_id,
                    ordinal,
                    requested_at,
                    now,
                    result.status.value,
                    result.response_code,
                    result.latency_ms,
                    result.error_code,
                ),
            )
            if result.status == AttemptStatus.DELIVERED:
                conn.execute(
                    "UPDATE autonomous_notifications SET status = 'delivered', attempts = ?, "
                    "next_attempt_at = NULL, delivered_at = ?, last_error_code = NULL, "
                    "updated_at = ? WHERE id = ?",
                    (ordinal, now, now, notification_id),
                )
            elif result.status == AttemptStatus.PERMANENT or ordinal >= MAX_NOTIFICATION_ATTEMPTS:
                conn.execute(
                    "UPDATE autonomous_notifications SET status = 'notification_failed', "
                    "attempts = ?, next_attempt_at = NULL, last_error_code = ?, updated_at = ? "
                    "WHERE id = ?",
                    (ordinal, result.error_code, now, notification_id),
                )
            else:
                created = _parse(current.created_at)
                target = created + timedelta(seconds=NOTIFICATION_SCHEDULE[ordinal])
                conn.execute(
                    "UPDATE autonomous_notifications SET attempts = ?, next_attempt_at = ?, "
                    "last_error_code = ?, updated_at = ? WHERE id = ?",
                    (
                        ordinal,
                        db_timestamp(max(target, moment)),
                        result.error_code,
                        now,
                        notification_id,
                    ),
                )
            updated = conn.execute(
                "SELECT * FROM autonomous_notifications WHERE id = ?", (notification_id,)
            ).fetchone()
            return _notification(updated)

        return self._write(write)

    def list_attempts(self, notification_id: str) -> list[dict[str, Any]]:
        def read(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT ordinal, status, response_code, error_code, latency_ms "
                "FROM autonomous_notification_attempts WHERE notification_id = ? ORDER BY ordinal",
                (notification_id,),
            ).fetchall()
            return [dict(row) for row in rows]

        return self._read(read)

    # Eventos da run -----------------------------------------------------------------------------

    def append_run_event(
        self, run_id: str, event_type: EventType, payload: dict[str, Any], key: str
    ) -> EventRecord:
        """Eventos autônomos entram no log da run, mesmo terminal, sempre com chave idempotente."""
        if event_type not in AUTONOMOUS_EVENTS:
            raise AutonomousError(QUEUE_UNAVAILABLE, f"{event_type.value} is not autonomous")
        event = prepare_event(event_type, payload=payload, idempotency_key=key)
        now = self._now()

        def write(conn: sqlite3.Connection) -> EventRecord:
            select_run(conn, run_id)
            return insert_event(conn, run_id, event, now)

        return self._write(write)


def _parse(value: str) -> datetime:
    from lastro.timeutil import parse_timestamp

    return parse_timestamp(value)


def _select_item(conn: sqlite3.Connection, item_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM autonomous_queue_items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise AutonomousError(QUEUE_UNAVAILABLE, f"queue item {item_id} not found")
    return row


def _select_schedule(conn: sqlite3.Connection, schedule_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM autonomous_schedules WHERE id = ?", (schedule_id,)).fetchone()
    if row is None:
        raise AutonomousError(QUEUE_UNAVAILABLE, f"schedule {schedule_id} not found")
    return row


def _position(conn: sqlite3.Connection, item: QueueItem) -> int | None:
    """Posição 1 é o item ativo; pendentes seguem em ordem FIFO."""
    if item.status.active:
        return 1
    if item.status != QueueStatus.QUEUED:
        return None
    active = conn.execute(
        "SELECT COUNT(*) FROM autonomous_queue_items WHERE project_id = ? AND profile_name = ? "
        "AND status IN ('claimed', 'running')",
        (item.project_id, item.profile),
    ).fetchone()[0]
    ahead = conn.execute(
        "SELECT COUNT(*) FROM autonomous_queue_items WHERE project_id = ? AND profile_name = ? "
        "AND status = 'queued' AND (queued_at < ? OR (queued_at = ? AND rowid < "
        "(SELECT rowid FROM autonomous_queue_items WHERE id = ?)))",
        (item.project_id, item.profile, item.queued_at, item.queued_at, item.id),
    ).fetchone()[0]
    return int(active) + int(ahead) + 1


def _require_lease(conn: sqlite3.Connection, lease: Lease, now: str) -> None:
    row = conn.execute(
        "SELECT token, generation, expires_at FROM autonomous_profile_leases "
        "WHERE project_id = ? AND profile_name = ?",
        (lease.project_id, lease.profile),
    ).fetchone()
    if (
        row is None
        or row["token"] != lease.token
        or int(row["generation"]) != lease.generation
        or row["expires_at"] is None
        or str(row["expires_at"]) <= now
    ):
        raise AutonomousError(LEASE_LOST, f"lease for profile {lease.profile} was lost")


def _select_comparison(
    conn: sqlite3.Connection, column: str, value: str
) -> StoredComparison | None:
    if column not in {"id", "queue_item_id", "run_id"}:
        raise ValueError(column)
    row = conn.execute(
        f"SELECT * FROM autonomous_comparisons WHERE {column} = ?", (value,)
    ).fetchone()
    if row is None:
        return None
    matches = conn.execute(
        "SELECT * FROM autonomous_finding_matches WHERE comparison_id = ? "
        "ORDER BY flow, label, current_finding_id, previous_finding_id, id",
        (row["id"],),
    ).fetchall()
    result = ComparisonResult(
        status=ComparisonStatus(row["status"]),
        run_id=row["run_id"],
        baseline_run_id=row["baseline_run_id"],
        algorithm=row["algorithm"],
        algorithm_sha256=row["algorithm_sha256"],
        coverage=_loads(row["coverage_json"]) or {},
        matches=tuple(_match(item) for item in matches),
        limitations=tuple(_loads(row["limitations_json"]) or ()),
    )
    return StoredComparison(row["id"], row["queue_item_id"], result, row["created_at"])


def _select_notifications(conn: sqlite3.Connection, item_id: str) -> list[NotificationRecord]:
    rows = conn.execute(
        "SELECT * FROM autonomous_notifications WHERE queue_item_id = ? ORDER BY channel_name, id",
        (item_id,),
    ).fetchall()
    return [_notification(row) for row in rows]
