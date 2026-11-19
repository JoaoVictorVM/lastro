"""Fatos declarados pelo autor da documentação e disputas entre fontes.

Documentação nunca vence: um bloco `lastro-fact` vira entrada `documentation` comparável na
memória, e uma divergência relatada pelo agente fica registrada com as duas versões, sem
escolher a verdadeira. Toda referência é resolvida e copiada pelo servidor; o agente só cita IDs
que esta run recebeu.
"""

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from pydantic import ValidationError

from lastro.history import knowledge
from lastro.history import sources as ledger
from lastro.history.errors import KnowledgeError
from lastro.history.redaction import canonical_json, redact_text, sha256_hex
from lastro.knowledge.models import (
    CATEGORY_FACTS,
    Actor,
    Category,
    JsonObject,
    JsonValue,
    Scope,
    SourceType,
)
from lastro.sources.models import (
    FACT_METADATA_INVALID,
    IDEMPOTENCY_CONFLICT,
    INVALID_SOURCE_REFERENCE,
    MAX_DECLARATION_CHARACTERS,
    SCOPE_MISMATCH,
    Assessment,
    ConflictReceipt,
    ConflictReportInput,
    ConflictSide,
    Declaration,
    DeclaredFact,
    ReferenceKind,
    SourcesError,
)

if TYPE_CHECKING:
    from lastro.history.store import HistoryStore

ADAPTER = "documentation"
# Documentação vale para o projeto inteiro; escopos mais específicos são comparados por covers.
PROJECT_SCOPE = Scope(None, None, None, None, None)
WITHDRAWN = "source_withdrawn"


def validate_declarations(declarations: tuple[Declaration, ...]) -> tuple[list[JsonValue], int]:
    """Blocos válidos prontos para o adaptador e quantos foram recusados.

    Bloco inválido não impede recuperar o texto do documento; só não cria fato.
    """
    valid: list[JsonValue] = []
    invalid = 0
    for item in declarations:
        if len(item.body) > MAX_DECLARATION_CHARACTERS:
            invalid += 1
            continue
        try:
            parsed = DeclaredFact.model_validate(json.loads(item.body))
            if parsed.fact.type not in CATEGORY_FACTS[parsed.category]:
                raise ValueError("fact does not belong to category")
            knowledge.check_directives(parsed.fact)
        except (ValueError, ValidationError, KnowledgeError):
            invalid += 1
            continue
        valid.append(
            {
                "heading": item.heading,
                "line": item.line,
                "flow": parsed.flow,
                "category": parsed.category.value,
                "fact": cast(JsonObject, parsed.fact.model_dump(mode="json")),
            }
        )
    return valid, invalid


def _declarations(text: str) -> list[JsonObject]:
    value = json.loads(text or "[]")
    items = cast(list[object], value) if isinstance(value, list) else []
    return [cast(JsonObject, item) for item in items if isinstance(item, dict)]


def _chunk_for(conn: sqlite3.Connection, document_id: str, chunking: str, heading: str) -> str:
    rows = conn.execute(
        "SELECT id, heading FROM source_chunks WHERE document_id = ? AND chunking_fingerprint = ? "
        "ORDER BY ordinal",
        (document_id, chunking),
    ).fetchall()
    chosen = next((row for row in rows if row["heading"] == heading), rows[0])
    return str(chosen["id"])


def declared_facts_hook(
    project_id: str,
) -> Callable[[sqlite3.Connection, str, str | None, str], None]:
    """Projeta os fatos declarados da geração publicada na memória, na mesma transação.

    Vínculos de documentos que saíram da geração ficam inativos; a entrada só é descartada se
    nenhuma outra fonte sustenta o mesmo valor, e nenhum conflito é resolvido por isso.
    """

    def hook(conn: sqlite3.Connection, generation_id: str, previous: str | None, now: str) -> None:
        fingerprint = json.loads(
            conn.execute(
                "SELECT fingerprint_json FROM source_generations WHERE id = ?", (generation_id,)
            ).fetchone()[0]
        )
        chunking = str(fingerprint.get("chunking", ""))
        kept: set[tuple[str, str]] = set()
        documents = conn.execute(
            "SELECT d.id, d.source_id, d.relative_path, d.file_sha256, d.declarations_json, "
            "s.display_label FROM source_generation_files f JOIN source_documents d "
            "ON d.id = f.document_id JOIN document_sources s ON s.id = d.source_id "
            "WHERE f.generation_id = ? AND f.status = 'ready' AND d.declarations_json <> '[]'",
            (generation_id,),
        ).fetchall()
        for document in documents:
            for item in _declarations(str(document["declarations_json"])):
                heading = str(item.get("heading", ""))
                chunk_id = _chunk_for(conn, str(document["id"]), chunking, heading)
                parsed = DeclaredFact.model_validate(
                    {"flow": item["flow"], "category": item["category"], "fact": item["fact"]}
                )
                path = f"{document['display_label']}/{document['relative_path']}"
                snapshot: JsonObject = {
                    "chunk_id": chunk_id,
                    "document_id": document["id"],
                    "source_id": document["source_id"],
                    "relative_path": document["relative_path"],
                    "heading": heading,
                    "citation": f"[{path} § {heading}]",
                    "file_sha256": document["file_sha256"],
                    "line": item.get("line"),
                }
                entry_id = _linked(conn, project_id, chunk_id, parsed)
                if entry_id is None:
                    try:
                        entry_id = knowledge.adapter_fact(
                            conn,
                            project_id,
                            adapter=ADAPTER,
                            source=SourceType.DOCUMENTATION,
                            source_ref=chunk_id,
                            flow=parsed.flow,
                            category=parsed.category,
                            fact=parsed.fact,
                            scope=PROJECT_SCOPE,
                            timestamp=now,
                            covering=True,
                        )
                    except KnowledgeError:
                        continue
                ledger.link_knowledge(
                    conn,
                    project_id,
                    entry_id,
                    f"{document['source_id']}:{document['relative_path']}",
                    chunk_id,
                    snapshot,
                    now,
                )
                kept.add((entry_id, chunk_id))
                for partner in knowledge.open_conflict_ids(conn, entry_id):
                    ledger.record_structured_conflict(
                        conn, project_id, None, parsed.flow, entry_id, partner, now
                    )
        _withdraw(conn, project_id, kept, now)

    return hook


def _linked(
    conn: sqlite3.Connection, project_id: str, chunk_id: str, parsed: DeclaredFact
) -> str | None:
    """Entrada ativa já vinculada a este chunk com o mesmo valor: nada a reprocessar."""
    from lastro.knowledge.facts import identity

    ident = identity(parsed.fact)
    row = conn.execute(
        "SELECT k.id FROM source_knowledge_refs r JOIN knowledge k ON k.id = r.knowledge_id "
        "WHERE r.project_id = ? AND r.chunk_identity = ? AND r.active = 1 "
        "AND k.discarded_at IS NULL AND k.flow = ? AND k.fact_key = ? AND k.value_sha256 = ?",
        (project_id, chunk_id, parsed.flow, ident.key, ident.value_sha256),
    ).fetchone()
    return str(row["id"]) if row is not None else None


def _withdraw(
    conn: sqlite3.Connection, project_id: str, kept: set[tuple[str, str]], now: str
) -> None:
    rows = conn.execute(
        "SELECT knowledge_id, chunk_identity FROM source_knowledge_refs "
        "WHERE project_id = ? AND active = 1",
        (project_id,),
    ).fetchall()
    touched: set[str] = set()
    for row in rows:
        pair = (str(row["knowledge_id"]), str(row["chunk_identity"]))
        if pair in kept:
            continue
        conn.execute(
            "UPDATE source_knowledge_refs SET active = 0 WHERE knowledge_id = ? "
            "AND chunk_identity = ?",
            pair,
        )
        touched.add(pair[0])
    for entry_id in sorted(touched):
        remaining = conn.execute(
            "SELECT 1 FROM source_knowledge_refs WHERE knowledge_id = ? AND active = 1 LIMIT 1",
            (entry_id,),
        ).fetchone()
        if remaining is None:
            knowledge.withdraw_documentation(conn, entry_id, now, WITHDRAWN)


# Disputas relatadas pelo agente -----------------------------------------------------------


def _normalize(text: str) -> str:
    return " ".join(text.split())


@dataclass(frozen=True)
class _Resolved:
    snapshot: JsonObject
    text: str
    knowledge_id: str | None = None
    flow: str | None = None


def _bad(message: str) -> SourcesError:
    return SourcesError(INVALID_SOURCE_REFERENCE, message)


def _external(store: "HistoryStore", run_id: str, side: ConflictSide) -> _Resolved | None:
    """Referências mantidas fora do catálogo documental, sempre presas a esta run."""
    from lastro.history.errors import MissionPlanError

    if side.kind == ReferenceKind.CRITERION:
        plan = store.get_active_mission_plan(run_id)
        criterion = next(
            (item for item in (plan.criteria if plan else ()) if item.id == side.reference_id),
            None,
        )
        if criterion is None or criterion.source not in ("mission_explicit", "clarification"):
            raise _bad("O critério não pertence ao plano ativo desta execução.")
        return _Resolved(
            {
                "kind": "criterion",
                "reference_id": criterion.id,
                "source_type": "issue" if criterion.source_ref else "mission",
                "source": criterion.source,
                "source_ref": criterion.source_ref,
                "text": criterion.text,
            },
            criterion.text,
        )
    if side.kind == ReferenceKind.CLARIFICATION:
        found = next(
            (item for item in store.list_clarifications(run_id) if item.id == side.reference_id),
            None,
        )
        if found is None or found.answer is None:
            raise _bad("A resposta humana não está vinculada a esta execução.")
        answer = canonical_json(found.answer)
        values = " ".join(str(value) for value in found.answer.values() if isinstance(value, str))
        return _Resolved(
            {
                "kind": "clarification",
                "reference_id": found.id,
                "source_type": "user",
                "question": found.question,
                "answer": found.answer,
            },
            values + " " + answer,
        )
    if side.kind == ReferenceKind.CONTROLLED_FILE:
        try:
            record, content = store.read_source_file(run_id, side.reference_id)
        except MissionPlanError:
            raise _bad("O arquivo não está no conjunto controlado desta execução.") from None
        return _Resolved(
            {
                "kind": "controlled_file",
                "reference_id": record.id,
                "source_type": "code",
                "authority": "inferred implementation",
                "path": record.relative_path,
                "content_sha256": record.content_sha256,
            },
            content,
        )
    return None


def _internal(
    conn: sqlite3.Connection, project_id: str, run_id: str, side: ConflictSide
) -> _Resolved:
    if side.kind == ReferenceKind.PASSAGE:
        row = conn.execute(
            "SELECT r.snapshot_json FROM source_run_refs r JOIN source_queries q "
            "ON q.id = r.query_id WHERE r.id = ? AND r.run_id = ? AND q.project_id = ?",
            (side.reference_id, run_id, project_id),
        ).fetchone()
        if row is None:
            raise _bad("A referência não pertence ao contexto autorizado desta execução.")
        snapshot = cast(JsonObject, json.loads(row["snapshot_json"]))
        return _Resolved(snapshot, str(snapshot.get("text", "")), flow=str(snapshot.get("flow")))
    if side.kind == ReferenceKind.KNOWLEDGE:
        consulted = conn.execute(
            "SELECT 1 FROM knowledge_run_refs WHERE run_id = ? AND knowledge_id = ? LIMIT 1",
            (run_id, side.reference_id),
        ).fetchone()
        if consulted is None:
            raise _bad("A entrada de memória não foi consultada nesta execução.")
        entry = knowledge.select_entry(conn, project_id, side.reference_id)
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        scope = knowledge.run_scope(run)
        if not (entry.scope.covers(scope) or scope.covers(entry.scope)):
            raise SourcesError(SCOPE_MISMATCH, "A entrada pertence a outro escopo.")
        snapshot = ledger.entry_snapshot(conn, entry.id)
        return _Resolved(
            snapshot,
            entry.statement + " " + canonical_json(entry.fact),
            knowledge_id=entry.id,
            flow=entry.flow,
        )
    if side.kind == ReferenceKind.OBSERVATION:
        row = conn.execute(
            "SELECT * FROM knowledge_observations WHERE id = ? AND run_id = ?",
            (side.reference_id, run_id),
        ).fetchone()
        if row is None:
            raise _bad("A observação não foi registrada nesta execução.")
        observation = knowledge.observation_from_row(row)
        labels = " ".join(str(item.get("label", "")) for item in observation.landmarks)
        projection = observation.projection
        fields = projection.get("fields")
        field_text = " ".join(
            " ".join(str(value) for value in cast(JsonObject, item).values())
            for item in (cast(list[JsonValue], fields) if isinstance(fields, list) else [])
            if isinstance(item, dict)
        )
        return _Resolved(
            {
                "kind": "observation",
                "reference_id": observation.id,
                "source_type": "observation",
                "route": observation.route,
                "screen_title": observation.screen_title,
                "content_sha256": observation.content_sha256,
                "observed_at": observation.observed_at,
            },
            " ".join((observation.screen_title, labels, field_text, canonical_json(projection))),
        )
    raise _bad("Tipo de referência desconhecido.")


def report_conflict(
    store: "HistoryStore", run_id: str, request: ConflictReportInput
) -> ConflictReceipt:
    """Registra a divergência relatada com snapshots das duas pontas copiados pelo servidor.

    Quote literal prova de onde veio cada trecho, não que a interpretação esteja correta: a
    disputa fica `reported`, nunca vira fato novo nem resolve nada.
    """
    external = {id(side): _external(store, run_id, side) for side in (request.left, request.right)}
    request_sha = sha256_hex(canonical_json(cast(JsonObject, request.model_dump(mode="json"))))
    timestamp = store.sources.now()
    project_id = store.project_id

    def write(conn: sqlite3.Connection) -> ConflictReceipt:
        previous = conn.execute(
            "SELECT request_sha256, conflict_id FROM source_conflict_requests "
            "WHERE run_id = ? AND idempotency_key = ?",
            (run_id, request.idempotency_key),
        ).fetchone()
        if previous is not None:
            if previous["request_sha256"] != request_sha:
                raise SourcesError(
                    IDEMPOTENCY_CONFLICT, "idempotency_key reutilizada com outro conteúdo."
                )
            return _receipt(conn, str(previous["conflict_id"]), deduplicated=True)
        sides: list[_Resolved] = []
        for side in (request.left, request.right):
            resolved = external[id(side)] or _internal(conn, project_id, run_id, side)
            if resolved.flow is not None and resolved.flow != request.flow:
                raise SourcesError(SCOPE_MISMATCH, "A referência pertence a outro fluxo.")
            if _normalize(side.quote) not in _normalize(resolved.text):
                raise _bad("O trecho citado não aparece literalmente na referência.")
            sides.append(
                _Resolved(
                    {**resolved.snapshot, "quote": redact_text(side.quote)},
                    resolved.text,
                    resolved.knowledge_id,
                    resolved.flow,
                )
            )
        knowledge_ids = [item.knowledge_id for item in sides if item.knowledge_id is not None]
        conflict_id, created = ledger.insert_conflict(
            conn,
            project_id=project_id,
            run_id=run_id,
            flow=request.flow,
            left=sides[0].snapshot,
            right=sides[1].snapshot,
            assessment=Assessment.REPORTED.value,
            explanation=request.explanation,
            knowledge_ids=knowledge_ids,
            timestamp=timestamp,
        )
        for entry_id in knowledge_ids:
            knowledge.contest_external(conn, entry_id, conflict_id, Actor.AGENT, timestamp, run_id)
        conn.execute(
            "INSERT INTO source_conflict_requests (run_id, idempotency_key, request_sha256, "
            "conflict_id) VALUES (?, ?, ?, ?)",
            (run_id, request.idempotency_key, request_sha, conflict_id),
        )
        return _receipt(conn, conflict_id, deduplicated=not created)

    return store.sources.transaction(write)


def _receipt(conn: sqlite3.Connection, conflict_id: str, *, deduplicated: bool) -> ConflictReceipt:
    row = conn.execute("SELECT * FROM source_conflicts WHERE id = ?", (conflict_id,)).fetchone()
    entries = tuple(
        str(item["knowledge_id"])
        for item in conn.execute(
            "SELECT knowledge_id FROM source_conflict_entries WHERE conflict_id = ? "
            "ORDER BY knowledge_id",
            (conflict_id,),
        )
    )
    return ConflictReceipt(
        conflict_id=conflict_id,
        status=str(row["status"]),
        assessment=Assessment(row["assessment"]),
        contested_knowledge_ids=entries,
        deduplicated=deduplicated,
    )


__all__ = [
    "FACT_METADATA_INVALID",
    "PROJECT_SCOPE",
    "Category",
    "declared_facts_hook",
    "report_conflict",
    "validate_declarations",
]
