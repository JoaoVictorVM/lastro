"""SQL interno do executor nativo: orçamentos, contexto da run e ledger de tentativas de modelo.

Nada aqui guarda prompt, resposta ou cabeçalho do provedor: só digests, contagens e valores em
nanoUSD. O comprometido é sempre derivado das linhas, nunca de acumuladores independentes.
"""

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Literal

from lastro.history.errors import HistoryError
from lastro.history.events import insert_event, prepare_event
from lastro.history.models import EventType, JsonObject, JsonValue
from lastro.history.redaction import canonical_json, sha256_hex

type CallStatus = Literal["reserved", "sent", "settled", "released", "uncertain"]
OPEN_STATUSES = ("reserved", "sent", "uncertain")


class BudgetError(HistoryError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class BudgetExceededError(BudgetError):
    def __init__(self, scope: str, committed: int, requested: int, cap: int) -> None:
        super().__init__(
            "cost_cap",
            f"{scope} budget cannot hold the next reservation "
            f"(committed {committed} + requested {requested} > cap {cap} nUSD)",
        )
        self.scope, self.committed, self.requested, self.cap = scope, committed, requested, cap


@dataclass(frozen=True)
class NativeContextInput:
    run_id: str
    cap_nusd: int
    suite_budget_id: str | None
    catalog: JsonValue
    catalog_sha256: str
    pricing: JsonValue
    pricing_sha256: str
    runtime_versions: JsonObject


@dataclass(frozen=True)
class ReservationInput:
    run_id: str
    step_index: int
    attempt: int
    node: str
    model: str
    payload_sha256: str
    max_output_tokens: int
    reserved_nusd: int


@dataclass(frozen=True)
class Settlement:
    input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    actual_nusd: int
    request_id: str | None
    latency_ms: int
    tool_calls: tuple[tuple[str, str], ...] = ()
    pricing_id: str = ""


@dataclass(frozen=True)
class ModelCall:
    id: str
    run_id: str
    step_index: int
    attempt: int
    node: str
    model: str
    status: CallStatus
    payload_sha256: str
    reserved_nusd: int
    actual_nusd: int | None
    input_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    output_tokens: int | None
    max_output_tokens: int
    request_id: str | None
    started_at: str
    finished_at: str | None
    latency_ms: int | None
    tool_calls: tuple[tuple[str, str], ...]
    error_code: str | None
    completion_event_id: int | None

    @property
    def committed_nusd(self) -> int:
        if self.status == "settled":
            return self.actual_nusd or 0
        if self.status == "released":
            return 0
        return self.reserved_nusd


@dataclass(frozen=True)
class NativeUsage:
    """Soma só usage conhecido; tentativas incertas ficam contadas à parte."""

    calls: int
    settled_calls: int
    uncertain_calls: int
    input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    confirmed_nusd: int
    committed_nusd: int
    cap_nusd: int

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def uncertain(self) -> bool:
        return self.uncertain_calls > 0


def _call_from_row(row: sqlite3.Row) -> ModelCall:
    tools = json.loads(row["tool_calls_json"])
    return ModelCall(
        id=row["id"],
        run_id=row["run_id"],
        step_index=row["step_index"],
        attempt=row["attempt"],
        node=row["node"],
        model=row["model"],
        status=row["status"],
        payload_sha256=row["payload_sha256"],
        reserved_nusd=row["reserved_nusd"],
        actual_nusd=row["actual_nusd"],
        input_tokens=row["input_tokens"],
        cache_read_tokens=row["cache_read_tokens"],
        cache_write_tokens=row["cache_write_tokens"],
        output_tokens=row["output_tokens"],
        max_output_tokens=row["max_output_tokens"],
        request_id=row["request_id"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        latency_ms=row["latency_ms"],
        tool_calls=tuple((str(item["id"]), str(item["name"])) for item in tools),
        error_code=row["error_code"],
        completion_event_id=row["completion_event_id"],
    )


def create_suite_budget(
    conn: sqlite3.Connection, project_id: str, cap_nusd: int, timestamp: str
) -> str:
    budget_id = f"suite:{uuid.uuid4()}"
    conn.execute(
        "INSERT INTO native_budgets (id, project_id, scope, run_id, cap_nusd, created_at) "
        "VALUES (?, ?, 'suite', NULL, ?, ?)",
        (budget_id, project_id, cap_nusd, timestamp),
    )
    return budget_id


def create_run_context(
    conn: sqlite3.Connection, project_id: str, data: NativeContextInput, timestamp: str
) -> None:
    """Orçamento da run e snapshot nativo; a suíte precisa ser do mesmo projeto."""
    run = conn.execute(
        "SELECT project_id, executor_name, skill_bundle_version FROM runs WHERE id = ?",
        (data.run_id,),
    ).fetchone()
    if run is None or run["project_id"] != project_id or run["executor_name"] != "native":
        raise BudgetError("native_context_mismatch", "run is not a native run of this project")
    if data.suite_budget_id is not None:
        suite = conn.execute(
            "SELECT project_id, scope FROM native_budgets WHERE id = ?", (data.suite_budget_id,)
        ).fetchone()
        if suite is None or suite["project_id"] != project_id or suite["scope"] != "suite":
            raise BudgetError("suite_budget_mismatch", "suite budget belongs to another project")
    budget_id = f"run:{data.run_id}"
    conn.execute(
        "INSERT INTO native_budgets (id, project_id, scope, run_id, cap_nusd, created_at) "
        "VALUES (?, ?, 'run', ?, ?, ?)",
        (budget_id, project_id, data.run_id, data.cap_nusd, timestamp),
    )
    conn.execute(
        "INSERT INTO native_run_contexts (run_id, run_budget_id, suite_budget_id, "
        "catalog_sha256, catalog_json, pricing_json, pricing_sha256, runtime_versions_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            data.run_id,
            budget_id,
            data.suite_budget_id,
            data.catalog_sha256,
            canonical_json(data.catalog),
            canonical_json(data.pricing),
            data.pricing_sha256,
            canonical_json(data.runtime_versions),
        ),
    )
    names: list[JsonValue] = []
    if isinstance(data.catalog, list):
        for item in data.catalog:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                names.append(item["name"])
    payload: JsonObject = {
        "skill_bundle_sha256": run["skill_bundle_version"],
        "catalog_sha256": data.catalog_sha256,
        "catalog_names": names,
        "pricing_sha256": data.pricing_sha256,
        "runtime_versions": data.runtime_versions,
        "suite_budget_id": data.suite_budget_id,
        "cost_cap_nusd": data.cap_nusd,
    }
    insert_event(
        conn, data.run_id, prepare_event(EventType.NATIVE_STARTED, payload=payload), timestamp
    )


_COMMITTED = (
    "COALESCE(SUM(CASE c.status WHEN 'settled' THEN c.actual_nusd "
    "WHEN 'released' THEN 0 ELSE c.reserved_nusd END), 0)"
)


def _run_committed(conn: sqlite3.Connection, run_id: str) -> int:
    row = conn.execute(
        f"SELECT {_COMMITTED} AS total FROM native_model_calls c WHERE c.run_id = ?", (run_id,)
    ).fetchone()
    return int(row["total"])


def _suite_committed(conn: sqlite3.Connection, suite_id: str) -> int:
    row = conn.execute(
        f"SELECT {_COMMITTED} AS total FROM native_model_calls c "
        "JOIN native_run_contexts x ON x.run_id = c.run_id WHERE x.suite_budget_id = ?",
        (suite_id,),
    ).fetchone()
    return int(row["total"])


def _cap(conn: sqlite3.Connection, budget_id: str) -> int:
    row = conn.execute("SELECT cap_nusd FROM native_budgets WHERE id = ?", (budget_id,)).fetchone()
    if row is None:
        raise BudgetError("budget_not_found", f"unknown budget {budget_id}")
    return int(row["cap_nusd"])


def _context(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM native_run_contexts WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise BudgetError("native_context_missing", f"run {run_id} has no native context")
    return row


def reserve(
    conn: sqlite3.Connection, data: ReservationInput, call_id: str, timestamp: str
) -> ModelCall:
    """Autoriza na mesma transação de escrita que lê as somas: dois processos não sobrepõem."""
    existing = conn.execute(
        "SELECT * FROM native_model_calls WHERE run_id = ? AND step_index = ? AND attempt = ?",
        (data.run_id, data.step_index, data.attempt),
    ).fetchone()
    if existing is not None:
        raise BudgetError("duplicate_reservation", "attempt already reserved")
    status = conn.execute("SELECT status FROM runs WHERE id = ?", (data.run_id,)).fetchone()
    if status is None or status["status"] in (
        "completed",
        "blocked",
        "interrupted",
        "cancelled",
        "limit_reached",
    ):
        raise BudgetError("run_not_active", "no reservation after the run ended")
    context = _context(conn, data.run_id)
    scopes = [("run", context["run_budget_id"], _run_committed(conn, data.run_id))]
    if context["suite_budget_id"] is not None:
        suite = context["suite_budget_id"]
        scopes.append(("suite", suite, _suite_committed(conn, suite)))
    for scope, budget_id, committed in scopes:
        cap = _cap(conn, budget_id)
        if committed + data.reserved_nusd > cap:
            raise BudgetExceededError(scope, committed, data.reserved_nusd, cap)
    conn.execute(
        "INSERT INTO native_model_calls (id, run_id, step_index, attempt, node, model, status, "
        "payload_sha256, reserved_nusd, max_output_tokens, started_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?)",
        (
            call_id,
            data.run_id,
            data.step_index,
            data.attempt,
            data.node,
            data.model,
            data.payload_sha256,
            data.reserved_nusd,
            data.max_output_tokens,
            timestamp,
        ),
    )
    return select_call(conn, call_id)


def select_call(conn: sqlite3.Connection, call_id: str) -> ModelCall:
    row = conn.execute("SELECT * FROM native_model_calls WHERE id = ?", (call_id,)).fetchone()
    if row is None:
        raise BudgetError("call_not_found", f"unknown model call {call_id}")
    return _call_from_row(row)


def select_calls(conn: sqlite3.Connection, run_id: str) -> list[ModelCall]:
    rows = conn.execute(
        "SELECT * FROM native_model_calls WHERE run_id = ? ORDER BY step_index, attempt",
        (run_id,),
    ).fetchall()
    return [_call_from_row(row) for row in rows]


def _transition(
    conn: sqlite3.Connection,
    call_id: str,
    allowed: tuple[str, ...],
    sql: str,
    params: tuple[object, ...],
) -> bool:
    placeholders = ", ".join("?" for _ in allowed)
    cursor = conn.execute(
        f"UPDATE native_model_calls SET {sql} WHERE id = ? AND status IN ({placeholders})",
        (*params, call_id, *allowed),
    )
    return cursor.rowcount == 1


def _base_payload(call: ModelCall) -> JsonObject:
    return {
        "schema_version": 1,
        "call_id": call.id,
        "run_id": call.run_id,
        "node": call.node,
        "step_index": call.step_index,
        "attempt": call.attempt,
        "model": call.model,
        "reserved_cost_usd": _usd(call.reserved_nusd),
    }


def _usd(value: int) -> str:
    from lastro.native.pricing import format_usd

    return format_usd(value)


def mark_sent(conn: sqlite3.Connection, call_id: str, timestamp: str) -> ModelCall:
    """Antes do envio; depois disso, sem prova de não cobrança, o valor nunca é liberado."""
    if not _transition(conn, call_id, ("reserved",), "status = 'sent'", ()):
        raise BudgetError("invalid_call_transition", "only a reserved attempt can be sent")
    call = select_call(conn, call_id)
    insert_event(
        conn,
        call.run_id,
        prepare_event(EventType.MODEL_CALL_STARTED, payload=_base_payload(call)),
        timestamp,
    )
    return call


def settle(conn: sqlite3.Connection, call_id: str, data: Settlement, timestamp: str) -> ModelCall:
    """Liquidação e evento de término juntos; repetir com o mesmo call_id é idempotente."""
    current = select_call(conn, call_id)
    if current.status == "settled":
        return current
    if current.status not in ("sent", "uncertain"):
        raise BudgetError("invalid_call_transition", "only a sent attempt can be settled")
    tools: list[JsonValue] = [{"id": tid, "name": name} for tid, name in data.tool_calls]
    payload = _base_payload(current) | {
        "request_id": data.request_id,
        # Contagens fora de chaves com "token": a redação central trataria como segredo.
        "usage": {
            "input": data.input_tokens,
            "cache_read_input": data.cache_read_tokens,
            "cache_creation_input": data.cache_write_tokens,
            "total_input": data.input_tokens + data.cache_read_tokens + data.cache_write_tokens,
            "output": data.output_tokens,
        },
        "latency_ms": data.latency_ms,
        "cost_usd": _usd(data.actual_nusd),
        "pricing_id": data.pricing_id,
        "tool_calls": tools,
        "usage_status": "reported",
    }
    event = insert_event(
        conn,
        current.run_id,
        prepare_event(EventType.MODEL_CALL_COMPLETED, payload=payload),
        timestamp,
    )
    _transition(
        conn,
        call_id,
        ("sent", "uncertain"),
        "status = 'settled', actual_nusd = ?, input_tokens = ?, cache_read_tokens = ?, "
        "cache_write_tokens = ?, output_tokens = ?, request_id = ?, finished_at = ?, "
        "latency_ms = ?, tool_calls_json = ?, error_code = NULL, completion_event_id = ?",
        (
            data.actual_nusd,
            data.input_tokens,
            data.cache_read_tokens,
            data.cache_write_tokens,
            data.output_tokens,
            data.request_id,
            timestamp,
            data.latency_ms,
            canonical_json(tools),
            event.id,
        ),
    )
    return select_call(conn, call_id)


def fail(
    conn: sqlite3.Connection,
    call_id: str,
    *,
    released: bool,
    error_code: str,
    latency_ms: int | None,
    request_id: str | None,
    timestamp: str,
) -> ModelCall:
    """Falha da tentativa: libera só com prova de não cobrança, senão retém como incerta."""
    current = select_call(conn, call_id)
    if current.status in ("settled", "released", "uncertain"):
        return current
    target = "released" if released or current.status == "reserved" else "uncertain"
    payload = _base_payload(current) | {
        "request_id": request_id,
        "latency_ms": latency_ms,
        "error_code": error_code,
        "usage_status": "not_charged" if target == "released" else "uncertain",
        "usage": None,
        "cost_usd": None,
    }
    event_id: int | None = None
    if current.status == "sent":
        event_id = insert_event(
            conn,
            current.run_id,
            prepare_event(EventType.MODEL_CALL_FAILED, payload=payload),
            timestamp,
        ).id
    _transition(
        conn,
        call_id,
        ("reserved", "sent"),
        "status = ?, error_code = ?, latency_ms = ?, request_id = ?, finished_at = ?, "
        "completion_event_id = ?",
        (target, error_code, latency_ms, request_id, timestamp, event_id),
    )
    return select_call(conn, call_id)


def usage(conn: sqlite3.Connection, run_id: str) -> NativeUsage:
    calls = select_calls(conn, run_id)
    context = _context(conn, run_id)
    settled = [call for call in calls if call.status == "settled"]
    return NativeUsage(
        calls=len(calls),
        settled_calls=len(settled),
        uncertain_calls=sum(1 for call in calls if call.status in ("uncertain", "sent")),
        input_tokens=sum(call.input_tokens or 0 for call in settled),
        cache_read_tokens=sum(call.cache_read_tokens or 0 for call in settled),
        cache_write_tokens=sum(call.cache_write_tokens or 0 for call in settled),
        output_tokens=sum(call.output_tokens or 0 for call in settled),
        confirmed_nusd=sum(call.actual_nusd or 0 for call in settled),
        committed_nusd=sum(call.committed_nusd for call in calls),
        cap_nusd=_cap(conn, context["run_budget_id"]),
    )


def has_context(conn: sqlite3.Connection, run_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM native_run_contexts WHERE run_id = ?", (run_id,)).fetchone()
    return row is not None


def reconcile_terminal(conn: sqlite3.Connection, timestamp: str) -> list[str]:
    """Runs já terminais: reserva nunca enviada é liberada, enviada sem resposta fica incerta.

    O registro terminal não é reaberto e nenhuma ação é repetida por esta recuperação.
    """
    rows = conn.execute(
        "SELECT c.id, c.status FROM native_model_calls c JOIN runs r ON r.id = c.run_id "
        "WHERE c.status IN ('reserved', 'sent') AND r.status IN "
        "('completed', 'blocked', 'interrupted', 'cancelled', 'limit_reached')"
    ).fetchall()
    changed: list[str] = []
    for row in rows:
        target = "released" if row["status"] == "reserved" else "uncertain"
        if _transition(
            conn,
            row["id"],
            (row["status"],),
            "status = ?, error_code = COALESCE(error_code, 'owner_lost'), finished_at = ?",
            (target, timestamp),
        ):
            changed.append(row["id"])
    return changed


def payload_digest(data: JsonValue) -> str:
    return sha256_hex(canonical_json(data))
