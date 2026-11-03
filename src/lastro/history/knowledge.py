"""SQL da memória: observações, propostas, consolidação, revisões, referências e mutações.

Tudo roda dentro das transações abertas pela fachada. Textos vindos da página ou do agente são
redigidos antes de hash e persistência, e os mesmos bytes servem para comparar e auditar.
"""

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime
from typing import cast
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from lastro.history.errors import KnowledgeError, RunNotFoundError
from lastro.history.events import insert_event, prepare_event
from lastro.history.models import EventType, JsonObject, JsonValue, RunStatus
from lastro.history.redaction import canonical_json, redact, redact_text, sha256_hex
from lastro.knowledge.consolidation import ActiveValue, decide, outcome
from lastro.knowledge.facts import (
    fact_json,
    identity,
    quoted_texts,
    render_statement,
    routes,
    search_text,
)
from lastro.knowledge.models import (
    CATEGORY_FACTS,
    FACT_SCHEMA_VERSION,
    Actor,
    Category,
    ClarificationFact,
    ConsolidationAttempt,
    EditDocument,
    ExperienceContext,
    ExperienceFact,
    Fact,
    FieldPropertyFact,
    HistoryOperation,
    KnowledgeDetail,
    KnowledgeEntry,
    KnowledgeEvidence,
    KnowledgeHistoryItem,
    KnowledgeMutation,
    MemoryOutcome,
    MemoryOutcomeKind,
    MutationOperation,
    MutationResult,
    ObservationRecord,
    ProposalInput,
    ProposalReceipt,
    QueryInput,
    QueryResult,
    RevalidateInput,
    RevalidationResult,
    Scope,
    SourceType,
    ValidationState,
    base_state,
)
from lastro.knowledge.retrieval import fit_entries, literal_terms, match_expression, staleness
from lastro.knowledge.revalidation import check
from lastro.knowledge.safety import INJECTION_MESSAGE, find_directive, wrap_untrusted
from lastro.timeutil import parse_timestamp

NO_ACTIVE_RUN = "no_active_run"
INVALID_KNOWLEDGE = "invalid_knowledge"
POSSIBLE_INJECTION = "possible_injection"
INVALID_REFERENCE = "invalid_reference"
SCOPE_MISMATCH = "scope_mismatch"
REVISION_CONFLICT = "revision_conflict"
MANUAL_VERIFICATION_REQUIRED = "manual_verification_required"
NOT_FOUND = "knowledge_not_found"
RUN_NOT_TERMINAL = "run_not_terminal"

PROJECTION_CHARACTER_LIMIT = 8000
TERMINAL_ATTEMPT = "terminal"
FACT_ADAPTER: TypeAdapter[Fact] = TypeAdapter(Fact)
NON_PROMOTING = frozenset({RunStatus.CANCELLED, RunStatus.INTERRUPTED})
_TERMINAL = tuple(
    status.value
    for status in (
        RunStatus.COMPLETED,
        RunStatus.BLOCKED,
        RunStatus.INTERRUPTED,
        RunStatus.CANCELLED,
        RunStatus.LIMIT_REACHED,
    )
)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _loads(text: str | None) -> JsonValue:
    if text is None:
        return None
    value: JsonValue = json.loads(text)
    return value


def _object(text: str | None) -> JsonObject:
    value = _loads(text)
    return cast(JsonObject, value) if isinstance(value, dict) else {}


def canonical_environment(url: str) -> str:
    """URL base sem credenciais, query ou fragmento; porta efetiva sempre explícita."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port or {"http": 80, "https": 443}.get(scheme)
    path = parts.path or "/"
    if not path.endswith("/"):
        path += "/"
    return f"{scheme}://{host}:{port}{path}"


def scope_from_json(data: JsonObject) -> Scope:
    def text(key: str) -> str | None:
        value = data.get(key)
        return value if isinstance(value, str) else None

    return Scope(
        environment_url=text("environment_url"),
        profile=text("profile"),
        tenant=text("tenant"),
        account=text("account"),
        origin_run_only=text("origin_run_only"),
    )


def scope_key(scope: Scope) -> str:
    return sha256_hex(canonical_json(redact(scope.as_json())))


def _run_row(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise RunNotFoundError(run_id)
    return row


def _active_run(conn: sqlite3.Connection, project_id: str, run_id: str) -> sqlite3.Row:
    row = _run_row(conn, run_id)
    if row["project_id"] != project_id or row["status"] in _TERMINAL:
        raise KnowledgeError(NO_ACTIVE_RUN, "No active run.")
    return row


def run_scope(run: sqlite3.Row, *, account: str | None = None) -> Scope:
    return Scope(
        environment_url=canonical_environment(str(run["environment_url"])),
        profile=str(run["profile"]),
        tenant=str(run["tenant"]),
        account=account,
        origin_run_only=None,
    )


def _open_conflicts(conn: sqlite3.Connection, entry_id: str) -> tuple[str, ...]:
    rows = conn.execute(
        "SELECT left_id, right_id FROM knowledge_conflicts "
        "WHERE resolved_at IS NULL AND (left_id = ? OR right_id = ?)",
        (entry_id, entry_id),
    ).fetchall()
    others = {row["right_id"] if row["left_id"] == entry_id else row["left_id"] for row in rows}
    return tuple(sorted(others))


def entry_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> KnowledgeEntry:
    return KnowledgeEntry(
        id=row["id"],
        project_id=row["project_id"],
        run_id=row["run_id"],
        flow=row["flow"],
        category=Category(row["category"]),
        fact=_object(row["fact_json"]),
        statement=row["statement"],
        validation_state=ValidationState(row["validation_state"]),
        source_type=SourceType(row["source_type"]),
        source_ref=row["source_ref"],
        scope=scope_from_json(_object(row["scope_json"])),
        system_version=row["system_version"],
        observed_at=row["observed_at"],
        last_verified_at=row["last_verified_at"],
        revision=row["revision"],
        discarded_at=row["discarded_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        conflict_ids=_open_conflicts(conn, row["id"]),
    )


def _entry_row(conn: sqlite3.Connection, project_id: str, entry_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM knowledge WHERE id = ? AND project_id = ? AND legacy = 0",
        (entry_id, project_id),
    ).fetchone()
    if row is None:
        raise KnowledgeError(NOT_FOUND, f"Unknown knowledge entry {entry_id}.")
    return row


def select_entry(conn: sqlite3.Connection, project_id: str, entry_id: str) -> KnowledgeEntry:
    return entry_from_row(conn, _entry_row(conn, project_id, entry_id))


def _history(
    conn: sqlite3.Connection,
    entry_id: str,
    operation: HistoryOperation,
    actor: Actor,
    revision: int,
    payload: JsonObject,
    timestamp: str,
    *,
    run_id: str | None = None,
    operation_key: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO knowledge_history (knowledge_id, run_id, payload_json, created_at, "
        "operation, actor, revision, operation_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            entry_id,
            run_id,
            canonical_json(redact(payload)),
            timestamp,
            operation.value,
            actor.value,
            revision,
            operation_key,
        ),
    )


def _event(conn: sqlite3.Connection, run_id: str, payload: JsonObject, timestamp: str) -> None:
    insert_event(
        conn, run_id, prepare_event(EventType.KNOWLEDGE_PROPOSED, payload=payload), timestamp
    )


# Observações -------------------------------------------------------------------------------


def observation_from_row(row: sqlite3.Row) -> ObservationRecord:
    landmarks = _loads(row["landmarks_json"])
    return ObservationRecord(
        id=row["id"],
        run_id=row["run_id"],
        event_id=row["event_id"],
        route=row["route"],
        screen_title=row["screen_title"],
        landmarks=tuple(
            cast(JsonObject, item)
            for item in (landmarks if isinstance(landmarks, list) else [])
            if isinstance(item, dict)
        ),
        scope=scope_from_json(_object(row["scope_json"])),
        projection=_object(row["projection_json"]),
        content_sha256=row["content_sha256"],
        observed_at=row["observed_at"],
    )


def _limit_projection(projection: JsonObject) -> JsonObject:
    """Corta listas inteiras até caber no teto; a projeção passa a ser marcada incompleta."""
    data = dict(projection)
    keys = [key for key in ("fields", "landmarks") if isinstance(data.get(key), list)]
    while len(canonical_json(data)) > PROJECTION_CHARACTER_LIMIT:
        trimmed = False
        for key in keys:
            items = cast(list[JsonValue], data[key])
            if items:
                data[key] = items[:-1]
                data["incomplete"] = True
                trimmed = True
                break
        if not trimmed:
            raise KnowledgeError(INVALID_KNOWLEDGE, "Observation projection exceeds the limit.")
    return data


def insert_observation(
    conn: sqlite3.Connection,
    project_id: str,
    run_id: str,
    action_id: str,
    projection: JsonObject,
    timestamp: str,
) -> ObservationRecord:
    run = _active_run(conn, project_id, run_id)
    event = conn.execute(
        "SELECT id FROM events WHERE run_id = ? AND correlation_id = ? AND event_type = ? "
        "ORDER BY id DESC LIMIT 1",
        (run_id, action_id, EventType.TOOL_CALL.value),
    ).fetchone()
    if event is None:
        raise KnowledgeError(INVALID_REFERENCE, "Observation needs its audited tool call.")
    data = _limit_projection(cast(JsonObject, redact(projection)))
    route = str(data.get("route") or "/")
    title = str(data.get("title") or "")
    raw_landmarks = data.get("landmarks")
    landmarks: list[JsonValue] = raw_landmarks if isinstance(raw_landmarks, list) else []
    scope = run_scope(run)
    record_id = new_id("obs")
    content = canonical_json(data)
    conn.execute(
        "INSERT INTO knowledge_observations (id, run_id, event_id, route, screen_title, "
        "landmarks_json, scope_json, projection_json, content_sha256, observed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            record_id,
            run_id,
            event["id"],
            route,
            title,
            canonical_json(landmarks),
            canonical_json(scope.as_json()),
            content,
            sha256_hex(content),
            timestamp,
        ),
    )
    return select_observation(conn, record_id)


def select_observation(conn: sqlite3.Connection, observation_id: str) -> ObservationRecord:
    row = conn.execute(
        "SELECT * FROM knowledge_observations WHERE id = ?", (observation_id,)
    ).fetchone()
    if row is None:
        raise KnowledgeError(INVALID_REFERENCE, f"Unknown observation {observation_id}.")
    return observation_from_row(row)


# Propostas ---------------------------------------------------------------------------------


def check_directives(fact: Fact) -> None:
    if find_directive(quoted_texts(fact)):
        raise KnowledgeError(POSSIBLE_INJECTION, INJECTION_MESSAGE)


def _clarification_answer(
    conn: sqlite3.Connection, project_id: str, clarification_id: str, fact: ClarificationFact
) -> ClarificationFact:
    row = conn.execute(
        "SELECT c.* FROM clarifications c JOIN runs r ON r.id = c.run_id "
        "WHERE c.id = ? AND r.project_id = ?",
        (clarification_id, project_id),
    ).fetchone()
    if row is None or row["status"] != "answered":
        raise KnowledgeError(INVALID_REFERENCE, "clarification_id must be an answered question.")
    if row["gap_id"] != fact.gap_id:
        raise KnowledgeError(INVALID_REFERENCE, "gap_id differs from the clarification.")
    stored = _object(row["answer_json"])
    if stored.get("source") != "user_provided":
        raise KnowledgeError(INVALID_REFERENCE, "The answer was not written by a human.")
    answer = redact_text(str(stored.get("answer") or ""))
    return fact.model_copy(update={"answer": answer})


def _check_observations(
    conn: sqlite3.Connection, run_id: str, ids: Iterable[str], fact: Fact
) -> list[ObservationRecord]:
    records: list[ObservationRecord] = []
    for observation_id in ids:
        record = select_observation(conn, observation_id)
        if record.run_id != run_id:
            raise KnowledgeError(INVALID_REFERENCE, "Observations must belong to this run.")
        records.append(record)
    observed = {record.route for record in records}
    # Metadados de tela vêm da observação do servidor; o agente não forja rota.
    if not set(routes(fact)) <= observed:
        raise KnowledgeError(INVALID_REFERENCE, "Routes must come from the cited observations.")
    if isinstance(fact, ExperienceFact):
        cited = next(r for r in records if r.id == fact.context.observation_id)
        if cited.route != fact.context.route:
            raise KnowledgeError(INVALID_REFERENCE, "Experience route differs from observation.")
    return records


def _check_artifacts(conn: sqlite3.Connection, run_id: str, ids: Iterable[str]) -> None:
    for artifact_id in ids:
        row = conn.execute("SELECT run_id, sha256 FROM artifacts WHERE id = ?", (artifact_id,))
        found = row.fetchone()
        if found is None or found["run_id"] != run_id:
            raise KnowledgeError(INVALID_REFERENCE, "Artifacts must belong to this run.")


def propose(
    conn: sqlite3.Connection,
    project_id: str,
    run_id: str,
    proposal: ProposalInput,
    timestamp: str,
) -> ProposalReceipt:
    """Valida proveniência e persiste a proposta com knowledge_proposed na mesma transação."""
    run = _active_run(conn, project_id, run_id)
    fact: Fact = proposal.fact
    check_directives(fact)
    if isinstance(fact, ClarificationFact):
        assert proposal.clarification_id is not None
        fact = _clarification_answer(conn, project_id, proposal.clarification_id, fact)
        source = SourceType.USER
        source_ref = proposal.clarification_id
    else:
        _check_observations(conn, run_id, proposal.observation_ids, fact)
        source = SourceType.OBSERVATION
        source_ref = proposal.observation_ids[0]
    _check_artifacts(conn, run_id, proposal.artifact_ids)
    try:
        render_statement(fact)
    except ValueError as exc:
        raise KnowledgeError(INVALID_KNOWLEDGE, str(exc)) from None
    scope = run_scope(run)
    if isinstance(fact, ExperienceFact) and fact.is_permission and scope.account is None:
        # Sem identidade observável, uma permissão vale só para a run de origem.
        scope = replace(scope, origin_run_only=run_id)
    payload = cast(
        JsonObject,
        redact(
            {
                "schema_version": FACT_SCHEMA_VERSION,
                "flow": proposal.flow,
                "category": proposal.category.value,
                "fact": fact.model_dump(mode="json"),
                "observation_ids": list(proposal.observation_ids),
                "artifact_ids": list(proposal.artifact_ids),
                "clarification_id": proposal.clarification_id,
                "source": source.value,
                "source_ref": source_ref,
                "scope": scope.as_json(),
                "system_version": run["executed_system_version"],
            }
        ),
    )
    content = canonical_json(payload)
    digest = sha256_hex(content)
    state = base_state(source)
    existing = conn.execute(
        "SELECT * FROM knowledge_pending WHERE run_id = ? AND idempotency_key = ?",
        (run_id, proposal.idempotency_key),
    ).fetchone()
    if existing is not None:
        if existing["content_sha256"] != digest:
            raise KnowledgeError(
                REVISION_CONFLICT, "idempotency_key was already used with different content."
            )
        return ProposalReceipt(existing["id"], source, state, deduplicated=True)
    stored_fact = FACT_ADAPTER.validate_python(payload["fact"])
    ident = identity(stored_fact)
    duplicate = conn.execute(
        "SELECT 1 FROM knowledge WHERE project_id = ? AND flow = ? AND scope_key = ? "
        "AND fact_key = ? AND value_sha256 = ? AND legacy = 0 AND discarded_at IS NULL",
        (project_id, proposal.flow, scope_key(scope), ident.key, ident.value_sha256),
    ).fetchone()
    proposal_id = new_id("kp")
    conn.execute(
        "INSERT INTO knowledge_pending (id, run_id, idempotency_key, payload_json, "
        "content_sha256, status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
        (proposal_id, run_id, proposal.idempotency_key, content, digest, timestamp),
    )
    _event(
        conn,
        run_id,
        {
            "subtype": "accepted",
            "proposal_id": proposal_id,
            "idempotency_key": proposal.idempotency_key,
            "content_sha256": digest,
            "source": source.value,
        },
        timestamp,
    )
    return ProposalReceipt(proposal_id, source, state, deduplicated=duplicate is not None)


def record_rejection(
    conn: sqlite3.Connection, run_id: str, code: str, raw: object, timestamp: str
) -> None:
    """Auditoria da recusa: código e hash do pedido redigido, nunca o texto bruto."""
    digest = sha256_hex(canonical_json(redact(raw)))
    _event(conn, run_id, {"subtype": code, "code": code, "arguments_sha256": digest}, timestamp)


# Busca -------------------------------------------------------------------------------------


def _verified_in_run(conn: sqlite3.Connection, run_id: str, entry: KnowledgeEntry) -> bool:
    row = conn.execute(
        "SELECT 1 FROM knowledge_history WHERE knowledge_id = ? AND run_id = ? "
        "AND operation = 'revalidated' AND revision = ?",
        (entry.id, run_id, entry.revision),
    ).fetchone()
    return row is not None


def entry_view(
    entry: KnowledgeEntry,
    now: datetime,
    current_version: str | None,
    requires_revalidation: bool,
) -> JsonObject:
    verified = parse_timestamp(entry.last_verified_at) if entry.last_verified_at else None
    stale = staleness(verified, now, entry.system_version, current_version)
    contested = entry.validation_state == ValidationState.CONTESTED
    return {
        "id": entry.id,
        "revision": entry.revision,
        "flow": entry.flow,
        "statement": entry.statement,
        "validation_state": entry.validation_state.value,
        "source": entry.source_type.value,
        "last_verified_at": entry.last_verified_at,
        "stale": stale.stale,
        "stale_reasons": list(stale.reasons),
        "version_unknown": stale.version_unknown,
        "requires_revalidation": requires_revalidation or contested or stale.stale,
        "conflict_ids": list(entry.conflict_ids),
        "source_ref": entry.source_ref,
        "scope": entry.scope.as_json(),
        "system_version": entry.system_version,
    }


def _candidates(
    conn: sqlite3.Connection, project_id: str, flow: str, query: str
) -> list[sqlite3.Row]:
    expression = match_expression(literal_terms(query))
    base = "k.project_id = ? AND k.flow = ? AND k.legacy = 0 AND k.discarded_at IS NULL"
    if expression is None:
        return conn.execute(
            f"SELECT k.* FROM knowledge k WHERE {base} "
            "ORDER BY k.last_verified_at IS NULL, k.last_verified_at DESC, k.id",
            (project_id, flow),
        ).fetchall()
    return conn.execute(
        "SELECT k.* FROM knowledge_fts f JOIN knowledge k ON k.id = f.entry_id "
        f"WHERE knowledge_fts MATCH ? AND {base} ORDER BY bm25(knowledge_fts), k.id",
        (expression, project_id, flow),
    ).fetchall()


def query(
    conn: sqlite3.Connection,
    project_id: str,
    run_id: str,
    request: QueryInput,
    now: datetime,
    timestamp: str,
) -> QueryResult:
    """Filtra por projeto, fluxo e escopo da run e fixa a revisão lida na mesma transação."""
    run = _active_run(conn, project_id, run_id)
    scope = run_scope(run)
    run_only = replace(scope, origin_run_only=run_id)
    views: list[JsonObject] = []
    entries: dict[str, KnowledgeEntry] = {}
    for row in _candidates(conn, project_id, request.flow, request.query):
        entry = entry_from_row(conn, row)
        if not (entry.scope.covers(scope) or entry.scope.covers(run_only)):
            continue
        entries[entry.id] = entry
        required = not _verified_in_run(conn, run_id, entry)
        views.append(entry_view(entry, now, run["executed_system_version"], required))
    budget = fit_entries(views, request.limit)
    for view in budget.included:
        entry_id = str(view["id"])
        conn.execute(
            "INSERT OR IGNORE INTO knowledge_run_refs (run_id, knowledge_id, revision, "
            "snapshot_json, accessed_at) VALUES (?, ?, ?, ?, ?)",
            (run_id, entry_id, entries[entry_id].revision, canonical_json(view), timestamp),
        )
    return QueryResult(
        content=wrap_untrusted(budget.content),
        returned_count=len(budget.included),
        content_characters=len(budget.content),
        truncated=budget.omitted > 0,
        omitted_count=budget.omitted,
    )


# Consolidação ------------------------------------------------------------------------------


def _set_fts(conn: sqlite3.Connection, entry_id: str, statement: str | None, text: str) -> None:
    conn.execute("DELETE FROM knowledge_fts WHERE entry_id = ?", (entry_id,))
    if statement is not None:
        conn.execute(
            "INSERT INTO knowledge_fts (entry_id, statement, search_text) VALUES (?, ?, ?)",
            (entry_id, statement, text),
        )


def _add_evidence(
    conn: sqlite3.Connection,
    entry_id: str,
    run_id: str,
    timestamp: str,
    *,
    observation_id: str | None = None,
    artifact_id: str | None = None,
    clarification_id: str | None = None,
) -> bool:
    cursor = conn.execute(
        "INSERT OR IGNORE INTO knowledge_evidence (id, knowledge_id, run_id, observation_id, "
        "artifact_id, clarification_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            new_id("kev"),
            entry_id,
            run_id,
            observation_id,
            artifact_id,
            clarification_id,
            timestamp,
        ),
    )
    return cursor.rowcount > 0


def _payload_evidence(
    conn: sqlite3.Connection, entry_id: str, run_id: str, payload: JsonObject, timestamp: str
) -> int:
    added = 0
    for key in ("observation_ids", "artifact_ids"):
        raw = payload.get(key)
        for ref in raw if isinstance(raw, list) else []:
            if not isinstance(ref, str):
                continue
            if key == "observation_ids":
                added += _add_evidence(conn, entry_id, run_id, timestamp, observation_id=ref)
            else:
                added += _add_evidence(conn, entry_id, run_id, timestamp, artifact_id=ref)
    clarification = payload.get("clarification_id")
    if isinstance(clarification, str):
        added += _add_evidence(conn, entry_id, run_id, timestamp, clarification_id=clarification)
    return added


def _bump(conn: sqlite3.Connection, entry_id: str, timestamp: str, **columns: object) -> int:
    assignments = "".join(f", {name} = ?" for name in columns)
    conn.execute(
        f"UPDATE knowledge SET revision = revision + 1, updated_at = ?{assignments} WHERE id = ?",
        (timestamp, *columns.values(), entry_id),
    )
    row = conn.execute("SELECT revision FROM knowledge WHERE id = ?", (entry_id,)).fetchone()
    return int(row["revision"])


def _contest(
    conn: sqlite3.Connection,
    entry_id: str,
    others: Iterable[str],
    actor: Actor,
    timestamp: str,
    run_id: str | None,
) -> None:
    for other in others:
        left, right = sorted((entry_id, other))
        conn.execute(
            "INSERT OR IGNORE INTO knowledge_conflicts (left_id, right_id, created_at) "
            "VALUES (?, ?, ?)",
            (left, right, timestamp),
        )
    for target in (entry_id, *others):
        row = conn.execute(
            "SELECT validation_state FROM knowledge WHERE id = ?", (target,)
        ).fetchone()
        if row["validation_state"] == ValidationState.CONTESTED.value:
            continue
        revision = _bump(conn, target, timestamp, validation_state=ValidationState.CONTESTED.value)
        group: list[JsonValue] = [item for item in (entry_id, *others) if item != target]
        _history(
            conn,
            target,
            HistoryOperation.CONTESTED,
            actor,
            revision,
            {"alternatives": group},
            timestamp,
            run_id=run_id,
        )


def _restore_state(conn: sqlite3.Connection, entry_id: str, timestamp: str) -> None:
    """Sem conflito aberto, a entrada volta ao estado da sua origem."""
    row = conn.execute(
        "SELECT validation_state, source_type FROM knowledge WHERE id = ?", (entry_id,)
    ).fetchone()
    if _open_conflicts(conn, entry_id):
        return
    target = base_state(SourceType(row["source_type"])).value
    if row["validation_state"] != target:
        conn.execute(
            "UPDATE knowledge SET validation_state = ?, updated_at = ? WHERE id = ?",
            (target, timestamp, entry_id),
        )


def _actives(
    conn: sqlite3.Connection, project_id: str, flow: str, key_scope: str, fact_key: str
) -> list[ActiveValue]:
    rows = conn.execute(
        "SELECT id, value_sha256 FROM knowledge WHERE project_id = ? AND flow = ? "
        "AND scope_key = ? AND fact_key = ? AND legacy = 0 AND discarded_at IS NULL ORDER BY id",
        (project_id, flow, key_scope, fact_key),
    ).fetchall()
    return [ActiveValue(row["id"], row["value_sha256"]) for row in rows]


def create_entry(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    run_id: str | None,
    flow: str,
    category: Category,
    fact: Fact,
    source: SourceType,
    source_ref: str,
    scope: Scope,
    system_version: str | None,
    observed_at: str,
    last_verified_at: str | None,
    actor: Actor,
    timestamp: str,
    conflicts_with: tuple[str, ...] = (),
    history_payload: JsonObject | None = None,
) -> str:
    if fact.type not in CATEGORY_FACTS[category]:
        raise KnowledgeError(INVALID_KNOWLEDGE, f"{fact.type} does not belong to {category}.")
    ident = identity(fact)
    statement = render_statement(fact)
    entry_id = new_id("kn")
    scope_json = cast(JsonObject, redact(scope.as_json()))
    conn.execute(
        "INSERT INTO knowledge (id, project_id, run_id, validation_state, payload_json, "
        "created_at, updated_at, flow, scope_json, scope_key, category, statement, fact_json, "
        "fact_key, value_sha256, source_type, source_ref, system_version, observed_at, "
        "last_verified_at, revision, legacy) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0)",
        (
            entry_id,
            project_id,
            run_id,
            base_state(source).value,
            canonical_json({"schema_version": FACT_SCHEMA_VERSION}),
            timestamp,
            timestamp,
            flow,
            canonical_json(scope_json),
            scope_key(scope),
            category.value,
            statement,
            canonical_json(cast(JsonObject, redact(fact.model_dump(mode="json")))),
            ident.key,
            ident.value_sha256,
            source.value,
            source_ref,
            system_version,
            observed_at,
            last_verified_at,
        ),
    )
    _set_fts(conn, entry_id, statement, search_text(flow, fact))
    _history(
        conn,
        entry_id,
        HistoryOperation.CREATED,
        actor,
        1,
        {"source": source.value, "source_ref": source_ref, **(history_payload or {})},
        timestamp,
        run_id=run_id,
    )
    if conflicts_with:
        _contest(conn, entry_id, conflicts_with, actor, timestamp, run_id)
    return entry_id


def _apply_proposal(
    conn: sqlite3.Connection, project_id: str, row: sqlite3.Row, timestamp: str
) -> bool:
    payload = _object(row["payload_json"])
    fact = FACT_ADAPTER.validate_python(payload["fact"])
    flow = str(payload["flow"])
    scope = scope_from_json(cast(JsonObject, payload["scope"]))
    ident = identity(fact)
    actives = _actives(conn, project_id, flow, scope_key(scope), ident.key)
    decision = decide(fact, ident.value_sha256, actives)
    run_id = str(row["run_id"])
    if decision.kind == "review":
        conn.execute(
            "UPDATE knowledge_pending SET status = 'review_required', reason = ?, "
            "processed_at = ? WHERE id = ?",
            (decision.reason, timestamp, row["id"]),
        )
        return False
    source = SourceType(str(payload["source"]))
    if decision.kind == "deduplicate":
        assert decision.entry_id is not None
        entry_id = decision.entry_id
        if _payload_evidence(conn, entry_id, run_id, payload, timestamp):
            current = conn.execute(
                "SELECT revision FROM knowledge WHERE id = ?", (entry_id,)
            ).fetchone()
            conn.execute("UPDATE knowledge SET updated_at = ? WHERE id = ?", (timestamp, entry_id))
            _history(
                conn,
                entry_id,
                HistoryOperation.EVIDENCE_ADDED,
                Actor.SYSTEM,
                int(current["revision"]),
                {"proposal_id": row["id"], "source": source.value},
                timestamp,
                run_id=run_id,
            )
    else:
        version = payload.get("system_version")
        entry_id = create_entry(
            conn,
            project_id=project_id,
            run_id=run_id,
            flow=flow,
            category=Category(str(payload["category"])),
            fact=fact,
            source=source,
            source_ref=str(payload["source_ref"]),
            scope=scope,
            system_version=version if isinstance(version, str) else None,
            observed_at=str(row["created_at"]),
            last_verified_at=str(row["created_at"]),
            actor=Actor.AGENT,
            timestamp=timestamp,
            conflicts_with=decision.conflicts_with,
            history_payload={"proposal_id": row["id"]},
        )
        _payload_evidence(conn, entry_id, run_id, payload, timestamp)
    conn.execute(
        "UPDATE knowledge_pending SET status = 'applied', entry_id = ?, processed_at = ? "
        "WHERE id = ?",
        (entry_id, timestamp, row["id"]),
    )
    return True


def consolidate_pending(
    conn: sqlite3.Connection, project_id: str, run_id: str, timestamp: str
) -> int:
    rows = conn.execute(
        "SELECT * FROM knowledge_pending WHERE run_id = ? AND status = 'pending' "
        "ORDER BY created_at, id",
        (run_id,),
    ).fetchall()
    return sum(_apply_proposal(conn, project_id, row, timestamp) for row in rows)


def _pending_count(conn: sqlite3.Connection, run_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM knowledge_pending WHERE run_id = ? "
        "AND status IN ('pending', 'review_required')",
        (run_id,),
    ).fetchone()
    return int(row["n"])


def _run_changes(conn: sqlite3.Connection, run_id: str) -> int:
    """Alterações efetivas já confirmadas durante a run, como revalidações."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM knowledge_history WHERE run_id = ? AND operation = ?",
        (run_id, HistoryOperation.REVALIDATED.value),
    ).fetchone()
    return int(row["n"])


def outcome_from_row(row: sqlite3.Row) -> MemoryOutcome:
    return MemoryOutcome(
        run_id=row["run_id"],
        outcome=MemoryOutcomeKind(row["outcome"]),
        revision=row["revision"],
        changed_count=row["changed_count"],
        pending_count=row["pending_count"],
        updated_at=row["updated_at"],
    )


def select_outcome(conn: sqlite3.Connection, run_id: str) -> MemoryOutcome | None:
    row = conn.execute("SELECT * FROM run_memory_outcomes WHERE run_id = ?", (run_id,)).fetchone()
    return outcome_from_row(row) if row is not None else None


def _attempt(
    conn: sqlite3.Connection,
    run_id: str,
    attempt_key: str,
    previous: MemoryOutcomeKind | None,
    result: MemoryOutcomeKind,
    error_code: str | None,
    timestamp: str,
) -> None:
    conn.execute(
        "INSERT INTO knowledge_consolidation_attempts (id, run_id, attempt_key, "
        "previous_outcome, outcome, error_code, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            new_id("kca"),
            run_id,
            attempt_key,
            previous.value if previous else None,
            result.value,
            error_code,
            timestamp,
        ),
    )


def _consolidate_guarded(
    conn: sqlite3.Connection, project_id: str, run_id: str, timestamp: str
) -> tuple[int, str | None]:
    """Savepoint: erro de domínio desfaz só a consolidação, nunca o término da run."""
    conn.execute("SAVEPOINT memory_consolidation")
    try:
        changed = consolidate_pending(conn, project_id, run_id, timestamp)
    except (KnowledgeError, ValidationError, ValueError, KeyError, sqlite3.IntegrityError) as exc:
        conn.execute("ROLLBACK TO memory_consolidation")
        conn.execute("RELEASE memory_consolidation")
        code = exc.code if isinstance(exc, KnowledgeError) else type(exc).__name__
        return 0, code
    conn.execute("RELEASE memory_consolidation")
    return changed, None


def finalize_memory(
    conn: sqlite3.Connection, run_id: str, target: RunStatus, timestamp: str
) -> MemoryOutcome:
    """Resultado único da run terminal; chamado pela transação que aplica o término."""
    existing = select_outcome(conn, run_id)
    if existing is not None:
        return existing
    project_id = str(_run_row(conn, run_id)["project_id"])
    changed = _run_changes(conn, run_id)
    error_code: str | None = None
    # Cancelamento e dono morto nunca promovem propostas ainda não consolidadas.
    if target not in NON_PROMOTING:
        promoted, error_code = _consolidate_guarded(conn, project_id, run_id, timestamp)
        changed += promoted
    pending = _pending_count(conn, run_id)
    if error_code is not None:
        pending = max(pending, 1)
    result = outcome(changed=changed, pending=pending)
    conn.execute(
        "INSERT INTO run_memory_outcomes (run_id, outcome, revision, changed_count, "
        "pending_count, updated_at) VALUES (?, ?, 1, ?, ?, ?)",
        (run_id, result.value, changed, pending, timestamp),
    )
    _attempt(conn, run_id, TERMINAL_ATTEMPT, None, result, error_code, timestamp)
    # nothing_new fica só na linha de resultado, preservando a sequência de eventos do término.
    if result != MemoryOutcomeKind.NOTHING_NEW:
        _event(
            conn,
            run_id,
            {
                "subtype": "consolidation",
                "outcome": result.value,
                "changed_count": changed,
                "pending_count": pending,
                "error_code": error_code,
            },
            timestamp,
        )
    drain_mutations(conn, project_id, timestamp)
    selected = select_outcome(conn, run_id)
    assert selected is not None
    return selected


def import_proposals(
    conn: sqlite3.Connection, run_id: str, proposals: list[JsonObject], timestamp: str
) -> int:
    """Importa propostas do fallback sem duplicar; hash divergente recusa o arquivo inteiro."""
    imported = 0
    for item in proposals:
        key = str(item["idempotency_key"])
        payload = cast(JsonObject, item["payload"])
        content = canonical_json(payload)
        digest = sha256_hex(content)
        if digest != item["content_sha256"]:
            raise KnowledgeError(INVALID_REFERENCE, "Fallback proposal hash differs.")
        row = conn.execute(
            "SELECT content_sha256 FROM knowledge_pending WHERE run_id = ? AND idempotency_key = ?",
            (run_id, key),
        ).fetchone()
        if row is not None:
            if row["content_sha256"] != digest:
                raise KnowledgeError(REVISION_CONFLICT, "Fallback proposal conflicts.")
            continue
        conn.execute(
            "INSERT INTO knowledge_pending (id, run_id, idempotency_key, payload_json, "
            "content_sha256, status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (str(item["id"]), run_id, key, content, digest, timestamp),
        )
        imported += 1
    return imported


def retry_consolidation(
    conn: sqlite3.Connection,
    project_id: str,
    run_id: str,
    attempt_key: str,
    timestamp: str,
    *,
    fallback: list[JsonObject] | None = None,
) -> MemoryOutcome:
    """Retry de run terminal: atualiza a linha corrente e guarda a anterior nas tentativas."""
    run = _run_row(conn, run_id)
    if run["project_id"] != project_id:
        raise KnowledgeError(SCOPE_MISMATCH, "The run belongs to another project.")
    if run["status"] not in _TERMINAL:
        raise KnowledgeError(RUN_NOT_TERMINAL, "Only terminal runs can be consolidated.")
    done = conn.execute(
        "SELECT 1 FROM knowledge_consolidation_attempts WHERE run_id = ? AND attempt_key = ?",
        (run_id, attempt_key),
    ).fetchone()
    current = select_outcome(conn, run_id)
    if done is not None and current is not None:
        return current
    imported = import_proposals(conn, run_id, fallback, timestamp) if fallback else 0
    previous = MemoryOutcomeKind.PERSISTENCE_FAILED if fallback is not None else None
    if current is not None and previous is None:
        previous = current.outcome
    promoted, error_code = _consolidate_guarded(conn, project_id, run_id, timestamp)
    pending = _pending_count(conn, run_id)
    if error_code is not None:
        pending = max(pending, 1)
    base = current.changed_count if current is not None else 0
    changed = base + promoted
    result = outcome(changed=changed, pending=pending)
    unchanged = (
        current is not None
        and fallback is None
        and promoted == 0
        and imported == 0
        and result == current.outcome
        and pending == current.pending_count
    )
    if unchanged and current is not None:
        return current
    if current is None:
        conn.execute(
            "INSERT INTO run_memory_outcomes (run_id, outcome, revision, changed_count, "
            "pending_count, updated_at) VALUES (?, ?, 1, ?, ?, ?)",
            (run_id, result.value, changed, pending, timestamp),
        )
    else:
        conn.execute(
            "UPDATE run_memory_outcomes SET outcome = ?, revision = revision + 1, "
            "changed_count = ?, pending_count = ?, updated_at = ? WHERE run_id = ?",
            (result.value, changed, pending, timestamp, run_id),
        )
    _attempt(conn, run_id, attempt_key, previous, result, error_code, timestamp)
    drain_mutations(conn, project_id, timestamp)
    selected = select_outcome(conn, run_id)
    assert selected is not None
    return selected


def select_attempts(conn: sqlite3.Connection, run_id: str) -> list[ConsolidationAttempt]:
    rows = conn.execute(
        "SELECT * FROM knowledge_consolidation_attempts WHERE run_id = ? ORDER BY created_at, id",
        (run_id,),
    ).fetchall()
    return [
        ConsolidationAttempt(
            id=row["id"],
            run_id=row["run_id"],
            attempt_key=row["attempt_key"],
            previous_outcome=(
                MemoryOutcomeKind(row["previous_outcome"]) if row["previous_outcome"] else None
            ),
            outcome=MemoryOutcomeKind(row["outcome"]),
            error_code=row["error_code"],
            created_at=row["created_at"],
        )
        for row in rows
    ]


def pending_snapshot(conn: sqlite3.Connection, run_id: str) -> list[JsonObject]:
    """Propostas confirmadas e ainda não consolidadas, para o fallback privado."""
    rows = conn.execute(
        "SELECT * FROM knowledge_pending WHERE run_id = ? AND status = 'pending' "
        "ORDER BY created_at, id",
        (run_id,),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "idempotency_key": row["idempotency_key"],
            "content_sha256": row["content_sha256"],
            "payload": _object(row["payload_json"]),
        }
        for row in rows
    ]


# Revalidação -------------------------------------------------------------------------------


def _mismatch_proposal(
    conn: sqlite3.Connection,
    run_id: str,
    entry: KnowledgeEntry,
    fact: Fact,
    observation: ObservationRecord,
    key: str,
    timestamp: str,
) -> str:
    """Divergência vira alternativa contextualizada pendente; a entrada original fica intacta."""
    alternative: Fact
    category = entry.category
    if isinstance(fact, FieldPropertyFact):
        alternative = fact.model_copy(update={"value": not fact.value})
    else:
        category = Category.EXPERIENCE
        alternative = ExperienceFact(
            type="experience",
            attempt="validate",
            outcome="failure",
            context=ExperienceContext(route=observation.route, observation_id=observation.id),
        )
    payload = cast(
        JsonObject,
        redact(
            {
                "schema_version": FACT_SCHEMA_VERSION,
                "flow": entry.flow,
                "category": category.value,
                "fact": alternative.model_dump(mode="json"),
                "observation_ids": [observation.id],
                "artifact_ids": [],
                "clarification_id": None,
                "source": SourceType.OBSERVATION.value,
                "source_ref": observation.id,
                "scope": entry.scope.as_json(),
                "system_version": None,
                "revalidates": entry.id,
            }
        ),
    )
    content = canonical_json(payload)
    proposal_key = "revalidate:" + key
    existing = conn.execute(
        "SELECT id FROM knowledge_pending WHERE run_id = ? AND idempotency_key = ?",
        (run_id, proposal_key[:100]),
    ).fetchone()
    if existing is not None:
        return str(existing["id"])
    proposal_id = new_id("kp")
    conn.execute(
        "INSERT INTO knowledge_pending (id, run_id, idempotency_key, payload_json, "
        "content_sha256, status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
        (proposal_id, run_id, proposal_key[:100], content, sha256_hex(content), timestamp),
    )
    return proposal_id


def revalidate(
    conn: sqlite3.Connection,
    project_id: str,
    run_id: str,
    request: RevalidateInput,
    timestamp: str,
) -> RevalidationResult:
    run = _active_run(conn, project_id, run_id)
    entry = select_entry(conn, project_id, request.entry_id)
    if entry.discarded_at is not None:
        raise KnowledgeError(INVALID_REFERENCE, "The entry was discarded.")
    scope = run_scope(run)
    run_only = replace(scope, origin_run_only=run_id)
    if not (entry.scope.covers(scope) or entry.scope.covers(run_only)):
        raise KnowledgeError(SCOPE_MISMATCH, "The entry belongs to another scope.")
    operation_key = f"revalidate:{run_id}:{request.idempotency_key}"
    replay = conn.execute(
        "SELECT 1 FROM knowledge_history WHERE operation_key = ?", (operation_key,)
    ).fetchone()
    if replay is not None:
        return RevalidationResult(
            "verified",
            entry.id,
            entry.revision,
            entry.last_verified_at,
            entry.validation_state,
            entry.validation_state == ValidationState.CONTESTED,
        )
    if entry.revision != request.expected_revision:
        raise KnowledgeError(
            REVISION_CONFLICT, "expected_revision is outdated.", current_revision=entry.revision
        )
    observation = select_observation(conn, request.observation_id)
    if observation.run_id != run_id or observation.scope.as_json() != scope.as_json():
        raise KnowledgeError(INVALID_REFERENCE, "The observation must belong to this run.")
    fact = FACT_ADAPTER.validate_python(entry.fact)
    verdict = check(fact, observation.projection)
    contested = entry.validation_state == ValidationState.CONTESTED
    if verdict in {"unsupported", "inconclusive"}:
        return RevalidationResult(
            "manual_verification_required",
            entry.id,
            entry.revision,
            entry.last_verified_at,
            entry.validation_state,
            True,
        )
    if verdict == "mismatch":
        proposal_id = _mismatch_proposal(
            conn, run_id, entry, fact, observation, request.idempotency_key, timestamp
        )
        _event(
            conn,
            run_id,
            {"subtype": "revalidation", "entry_id": entry.id, "result": "mismatch"},
            timestamp,
        )
        return RevalidationResult(
            "mismatch",
            entry.id,
            entry.revision,
            entry.last_verified_at,
            entry.validation_state,
            True,
            proposal_id=proposal_id,
        )
    revision = _bump(
        conn,
        entry.id,
        timestamp,
        last_verified_at=timestamp,
        system_version=run["executed_system_version"],
    )
    _add_evidence(conn, entry.id, run_id, timestamp, observation_id=observation.id)
    _history(
        conn,
        entry.id,
        HistoryOperation.REVALIDATED,
        Actor.AGENT,
        revision,
        {"observation_id": observation.id, "verdict": verdict},
        timestamp,
        run_id=run_id,
        operation_key=operation_key,
    )
    conn.execute(
        "INSERT OR IGNORE INTO knowledge_run_refs (run_id, knowledge_id, revision, "
        "snapshot_json, accessed_at) VALUES (?, ?, ?, ?, ?)",
        (run_id, entry.id, revision, canonical_json({"revalidated": True}), timestamp),
    )
    _event(
        conn,
        run_id,
        {"subtype": "revalidation", "entry_id": entry.id, "result": "verified"},
        timestamp,
    )
    return RevalidationResult(
        "verified",
        entry.id,
        revision,
        timestamp,
        entry.validation_state,
        contested,
    )


# Alterações humanas ------------------------------------------------------------------------


def _referencing_runs(conn: sqlite3.Connection, entry_id: str) -> list[str]:
    placeholders = ", ".join("?" for _ in _TERMINAL)
    rows = conn.execute(
        "SELECT DISTINCT r.id FROM knowledge_run_refs ref JOIN runs r ON r.id = ref.run_id "
        f"WHERE ref.knowledge_id = ? AND r.status NOT IN ({placeholders})",
        (entry_id, *_TERMINAL),
    ).fetchall()
    return [row["id"] for row in rows]


def _validate_mutation(
    conn: sqlite3.Connection,
    project_id: str,
    entry: KnowledgeEntry,
    operation: MutationOperation,
    payload: JsonObject,
) -> None:
    if entry.discarded_at is not None:
        raise KnowledgeError(INVALID_REFERENCE, "The entry was already discarded.")
    if operation != MutationOperation.EDIT:
        return
    try:
        document = EditDocument.model_validate(payload["document"])
    except (ValidationError, KeyError) as exc:
        raise KnowledgeError(INVALID_KNOWLEDGE, str(exc).splitlines()[0]) from None
    if document.fact.type not in CATEGORY_FACTS[entry.category]:
        raise KnowledgeError(INVALID_KNOWLEDGE, "The fact type must keep the entry category.")
    if isinstance(document.fact, ClarificationFact) and not document.fact.answer:
        raise KnowledgeError(INVALID_KNOWLEDGE, "Clarification edits need the answer text.")
    group = set(entry.conflict_ids)
    for alternative in document.discard_alternatives:
        if alternative not in group:
            raise KnowledgeError(
                INVALID_REFERENCE, f"{alternative} is not an open alternative of {entry.id}."
            )
        select_entry(conn, project_id, alternative)


def _discard(
    conn: sqlite3.Connection,
    entry_id: str,
    reason: str,
    actor: Actor,
    timestamp: str,
    extra: JsonObject | None = None,
) -> None:
    partners = _open_conflicts(conn, entry_id)
    conn.execute(
        "UPDATE knowledge_conflicts SET resolved_at = ?, resolution_reason = ? "
        "WHERE resolved_at IS NULL AND (left_id = ? OR right_id = ?)",
        (timestamp, redact_text(reason), entry_id, entry_id),
    )
    revision = _bump(conn, entry_id, timestamp, discarded_at=timestamp)
    _set_fts(conn, entry_id, None, "")
    _history(
        conn,
        entry_id,
        HistoryOperation.DISCARDED,
        actor,
        revision,
        {"reason": reason, **(extra or {})},
        timestamp,
    )
    for partner in partners:
        _restore_state(conn, partner, timestamp)


def apply_mutation(
    conn: sqlite3.Connection,
    project_id: str,
    entry_id: str,
    operation: MutationOperation,
    payload: JsonObject,
    reason: str,
    timestamp: str,
) -> None:
    entry = select_entry(conn, project_id, entry_id)
    if operation == MutationOperation.DISCARD:
        _discard(conn, entry.id, reason, Actor.USER, timestamp)
        return
    if operation == MutationOperation.REVALIDATE:
        version = payload.get("system_version")
        revision = _bump(
            conn,
            entry.id,
            timestamp,
            last_verified_at=timestamp,
            system_version=version if isinstance(version, str) else None,
        )
        _history(
            conn,
            entry.id,
            HistoryOperation.REVALIDATED,
            Actor.USER,
            revision,
            {"reason": reason, "attested": True, "system_version": version},
            timestamp,
        )
        return
    document = EditDocument.model_validate(payload["document"])
    fact = document.fact
    ident = identity(fact)
    clash = conn.execute(
        "SELECT id FROM knowledge WHERE project_id = ? AND flow = ? AND scope_key = ? "
        "AND fact_key = ? AND value_sha256 = ? AND legacy = 0 AND discarded_at IS NULL "
        "AND id <> ?",
        (
            project_id,
            entry.flow,
            scope_key(entry.scope),
            ident.key,
            ident.value_sha256,
            entry.id,
        ),
    ).fetchone()
    if clash is not None and clash["id"] not in document.discard_alternatives:
        raise KnowledgeError(INVALID_KNOWLEDGE, f"The same fact already exists as {clash['id']}.")
    for alternative in document.discard_alternatives:
        _discard(conn, alternative, reason, Actor.USER, timestamp, {"resolved_by_edit": entry.id})
    statement = render_statement(fact)
    before: JsonObject = {
        "fact": entry.fact,
        "statement": entry.statement,
        "source": entry.source_type.value,
    }
    revision = _bump(
        conn,
        entry.id,
        timestamp,
        fact_json=canonical_json(cast(JsonObject, redact(fact.model_dump(mode="json")))),
        fact_key=ident.key,
        value_sha256=ident.value_sha256,
        statement=statement,
        source_type=SourceType.USER.value,
        source_ref="cli",
    )
    _set_fts(conn, entry.id, statement, search_text(entry.flow, fact))
    _history(
        conn,
        entry.id,
        HistoryOperation.EDITED,
        Actor.USER,
        revision,
        {
            "reason": reason,
            "before": before,
            "after": {"fact": fact_json(fact), "statement": statement},
            "discarded_alternatives": [item for item in document.discard_alternatives],
        },
        timestamp,
    )
    # Corrigir o texto não resolve o grupo: só o descarte das alternativas fecha o conflito.
    _restore_state(conn, entry.id, timestamp)


def request_mutation(
    conn: sqlite3.Connection,
    project_id: str,
    entry_id: str,
    operation: MutationOperation,
    expected_revision: int,
    payload: JsonObject,
    reason: str,
    timestamp: str,
) -> MutationResult:
    """Aplica já, ou adia enquanto alguma run não terminal usa a entrada."""
    entry = select_entry(conn, project_id, entry_id)
    if entry.revision != expected_revision:
        raise KnowledgeError(
            REVISION_CONFLICT,
            f"Revision {expected_revision} is outdated; current revision is {entry.revision}.",
            current_revision=entry.revision,
        )
    _validate_mutation(conn, project_id, entry, operation, payload)
    if _referencing_runs(conn, entry.id):
        mutation_id = new_id("kmu")
        conn.execute(
            "INSERT INTO knowledge_mutations (id, knowledge_id, expected_revision, operation, "
            "payload_json, status, created_at, reason) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                mutation_id,
                entry.id,
                expected_revision,
                operation.value,
                canonical_json(cast(JsonObject, redact(payload))),
                timestamp,
                redact_text(reason),
            ),
        )
        _history(
            conn,
            entry.id,
            HistoryOperation.MUTATION_REQUESTED,
            Actor.USER,
            entry.revision,
            {"mutation_id": mutation_id, "operation": operation.value, "reason": reason},
            timestamp,
        )
        return MutationResult("deferred", select_entry(conn, project_id, entry.id), mutation_id)
    apply_mutation(conn, project_id, entry.id, operation, payload, reason, timestamp)
    return MutationResult("applied", select_entry(conn, project_id, entry.id))


def drain_mutations(conn: sqlite3.Connection, project_id: str, timestamp: str) -> int:
    """Aplica na ordem de criação as mutações cujas entradas nenhuma run ativa usa mais."""
    rows = conn.execute(
        "SELECT m.* FROM knowledge_mutations m JOIN knowledge k ON k.id = m.knowledge_id "
        "WHERE m.status = 'pending' AND k.project_id = ? ORDER BY m.created_at, m.id",
        (project_id,),
    ).fetchall()
    applied = 0
    for row in rows:
        if _referencing_runs(conn, row["knowledge_id"]):
            continue
        entry = select_entry(conn, project_id, row["knowledge_id"])
        payload = _object(row["payload_json"])
        operation = MutationOperation(row["operation"])
        reason = str(row["reason"] or "")
        error: str | None = None
        if entry.revision != row["expected_revision"]:
            error = REVISION_CONFLICT
        else:
            conn.execute("SAVEPOINT memory_mutation")
            try:
                _validate_mutation(conn, project_id, entry, operation, payload)
                apply_mutation(conn, project_id, entry.id, operation, payload, reason, timestamp)
            except KnowledgeError as exc:
                conn.execute("ROLLBACK TO memory_mutation")
                error = exc.code
            conn.execute("RELEASE memory_mutation")
        if error is None:
            conn.execute(
                "UPDATE knowledge_mutations SET status = 'applied', applied_at = ? WHERE id = ?",
                (timestamp, row["id"]),
            )
            applied += 1
            continue
        # Mutação desatualizada nunca sobrescreve uma revisão posterior.
        conn.execute(
            "UPDATE knowledge_mutations SET status = 'conflict', applied_at = ? WHERE id = ?",
            (timestamp, row["id"]),
        )
        _history(
            conn,
            entry.id,
            HistoryOperation.MUTATION_REJECTED,
            Actor.SYSTEM,
            entry.revision,
            {"mutation_id": row["id"], "error": error, "current_revision": entry.revision},
            timestamp,
        )
    return applied


# Leitura humana ----------------------------------------------------------------------------


def list_entries(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    flow: str | None = None,
    state: ValidationState | None = None,
    include_discarded: bool = False,
) -> list[KnowledgeEntry]:
    clauses = ["project_id = ?", "legacy = 0"]
    params: list[object] = [project_id]
    if flow is not None:
        clauses.append("flow = ?")
        params.append(flow)
    if state is not None:
        clauses.append("validation_state = ?")
        params.append(state.value)
    if not include_discarded:
        clauses.append("discarded_at IS NULL")
    rows = conn.execute(
        f"SELECT * FROM knowledge WHERE {' AND '.join(clauses)} ORDER BY flow, statement, id",
        params,
    ).fetchall()
    return [entry_from_row(conn, row) for row in rows]


def detail(conn: sqlite3.Connection, project_id: str, entry_id: str) -> KnowledgeDetail:
    entry = select_entry(conn, project_id, entry_id)
    evidence = conn.execute(
        "SELECT * FROM knowledge_evidence WHERE knowledge_id = ? ORDER BY created_at, id",
        (entry_id,),
    ).fetchall()
    history = conn.execute(
        "SELECT * FROM knowledge_history WHERE knowledge_id = ? ORDER BY id", (entry_id,)
    ).fetchall()
    mutations = conn.execute(
        "SELECT * FROM knowledge_mutations WHERE knowledge_id = ? ORDER BY created_at, id",
        (entry_id,),
    ).fetchall()
    return KnowledgeDetail(
        entry=entry,
        evidence=tuple(
            KnowledgeEvidence(
                id=row["id"],
                knowledge_id=row["knowledge_id"],
                run_id=row["run_id"],
                observation_id=row["observation_id"],
                artifact_id=row["artifact_id"],
                clarification_id=row["clarification_id"],
                created_at=row["created_at"],
            )
            for row in evidence
        ),
        history=tuple(
            KnowledgeHistoryItem(
                id=row["id"],
                knowledge_id=row["knowledge_id"],
                run_id=row["run_id"],
                operation=row["operation"],
                actor=row["actor"],
                revision=row["revision"],
                payload=_object(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in history
        ),
        alternatives=tuple(select_entry(conn, project_id, other) for other in entry.conflict_ids),
        mutations=tuple(
            KnowledgeMutation(
                id=row["id"],
                knowledge_id=row["knowledge_id"],
                expected_revision=row["expected_revision"],
                operation=MutationOperation(row["operation"]),
                payload=_object(row["payload_json"]),
                status=row["status"],
                created_at=row["created_at"],
                applied_at=row["applied_at"],
                reason=row["reason"],
            )
            for row in mutations
        ),
    )


def fts5_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.lastro_fts5_probe USING fts5(value)")
        conn.execute("DROP TABLE temp.lastro_fts5_probe")
    except sqlite3.OperationalError:
        return False
    return True
