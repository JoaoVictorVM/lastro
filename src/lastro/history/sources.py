"""Catálogo documental no histórico: raízes, gerações, documentos, chunks e citações por run.

Extração e embeddings acontecem fora daqui; cada função recebe dados prontos e roda dentro de
uma transação curta. A publicação troca a geração ativa com compare-and-swap da revisão e com o
token de fencing do lease, para que um escritor expirado nunca sobrescreva um mais novo.
"""

import json
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast
from uuid import uuid4

from lastro.history.database import Database, db_timestamp
from lastro.history.redaction import canonical_json, redact_text, sha256_hex
from lastro.knowledge.models import JsonObject, JsonValue
from lastro.sources.models import (
    CANDIDATES,
    CATALOG_REVISION_CONFLICT,
    INDEX_INCOMPATIBLE,
    INVALID_SOURCES_INPUT,
    LEASE_SECONDS,
    NO_MATCHES,
    NO_SOURCES,
    SOURCES_BUSY,
    SOURCES_UNAVAILABLE,
    Assessment,
    Candidate,
    ChunkDraft,
    FileStatus,
    Passage,
    QueryOutcome,
    SourceConflict,
    SourceRoot,
    SourcesError,
    SourceStatus,
    citation,
)
from lastro.sources.retrieval import fit_passages, fuse, match_expression

DOCUMENTATION = "documentation"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _object(text: str | None) -> JsonObject:
    if not text:
        return {}
    value = json.loads(text)
    return cast(JsonObject, value) if isinstance(value, dict) else {}


@dataclass(frozen=True)
class Lease:
    token: str
    generation: int
    revision: int
    staging_id: str
    staging_ordinal: int


@dataclass(frozen=True)
class ActiveFile:
    source_id: str
    relative_path: str
    status: FileStatus
    reason: str | None
    document_id: str | None
    file_sha256: str | None
    byte_size: int
    modified_at: str | None


@dataclass(frozen=True)
class ActiveGeneration:
    id: str
    ordinal: int
    fingerprint: JsonObject


@dataclass(frozen=True)
class DocumentDraft:
    source_id: str
    relative_path: str
    format: str
    file_sha256: str
    text_sha256: str
    byte_size: int
    normalized_text: str
    extraction_fingerprint: str
    declarations: list[JsonValue]


@dataclass(frozen=True)
class FileRow:
    source_id: str
    relative_path: str
    status: FileStatus
    document_id: str | None
    modified_at: str | None
    reason: str | None


type Publisher = Callable[[sqlite3.Connection, str, str | None, str], None]


def _row_root(row: sqlite3.Row) -> SourceRoot:
    return SourceRoot(
        id=row["id"],
        project_id=row["project_id"],
        display_label=row["display_label"],
        created_at=row["created_at"],
        last_sync_at=row["last_sync_at"],
        canonical_root=row["canonical_root"],
    )


class SourceRepository:
    """Fachada transacional das fontes de um projeto; nenhuma consulta aceita ID do agente."""

    def __init__(
        self,
        database: Database,
        retrieval: Callable[[], Database],
        project_id: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._database = database
        self._retrieval = retrieval
        self.project_id = project_id
        self._clock = clock

    def _now(self) -> str:
        return db_timestamp(self._clock())

    # Raízes -------------------------------------------------------------------------------

    def roots(self) -> list[SourceRoot]:
        return self._database.read(
            lambda conn: [
                _row_root(row)
                for row in conn.execute(
                    "SELECT * FROM document_sources WHERE project_id = ? ORDER BY created_at, id",
                    (self.project_id,),
                ).fetchall()
            ]
        )

    def register_root(self, canonical: str, key: str, label: str) -> tuple[SourceRoot, bool]:
        """Mesma raiz devolve o cadastro existente; rótulo repetido é uso inválido."""
        timestamp = self._now()

        def write(conn: sqlite3.Connection) -> tuple[SourceRoot, bool]:
            row = conn.execute(
                "SELECT * FROM document_sources WHERE project_id = ? AND root_key = ?",
                (self.project_id, key),
            ).fetchone()
            if row is not None:
                return _row_root(row), False
            taken = conn.execute(
                "SELECT 1 FROM document_sources WHERE project_id = ? AND display_label = ?",
                (self.project_id, label),
            ).fetchone()
            if taken is not None:
                raise SourcesError(
                    INVALID_SOURCES_INPUT,
                    f"Já existe uma fonte com o rótulo '{label}'; renomeie a pasta.",
                )
            source_id = new_id("src")
            conn.execute(
                "INSERT INTO document_sources (id, project_id, canonical_root, root_key, "
                "display_label, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (source_id, self.project_id, canonical, key, label, timestamp),
            )
            created = conn.execute(
                "SELECT * FROM document_sources WHERE id = ?", (source_id,)
            ).fetchone()
            return _row_root(created), True

        return self._database.write(write)

    def discard_root(self, source_id: str) -> None:
        """Desfaz um cadastro cuja primeira geração não foi publicada."""

        def write(conn: sqlite3.Connection) -> None:
            used = conn.execute(
                "SELECT 1 FROM source_generation_files WHERE source_id = ? LIMIT 1", (source_id,)
            ).fetchone()
            if used is not None:
                return
            _delete_documents(
                conn,
                [
                    str(row["id"])
                    for row in conn.execute(
                        "SELECT id FROM source_documents WHERE source_id = ?", (source_id,)
                    )
                ],
            )
            conn.execute(
                "DELETE FROM document_sources WHERE id = ? AND project_id = ?",
                (source_id, self.project_id),
            )

        self._database.write(write)

    # Lease --------------------------------------------------------------------------------

    def acquire(self, token: str, fingerprint: JsonObject) -> Lease:
        """Um escritor por projeto; staging órfão de lease vencido é limpo aqui."""
        now = self._clock()
        timestamp = db_timestamp(now)
        expires = db_timestamp(now + timedelta(seconds=LEASE_SECONDS))

        def write(conn: sqlite3.Connection) -> Lease:
            conn.execute(
                "INSERT OR IGNORE INTO source_catalogs (project_id, updated_at) VALUES (?, ?)",
                (self.project_id, timestamp),
            )
            row = conn.execute(
                "SELECT * FROM source_catalogs WHERE project_id = ?", (self.project_id,)
            ).fetchone()
            if row["lease_token"] is not None and row["lease_expires_at"] > timestamp:
                raise SourcesError(
                    SOURCES_BUSY, "Outra sincronização deste projeto está em andamento."
                )
            generation = int(row["lease_generation"]) + 1
            conn.execute(
                "UPDATE source_catalogs SET lease_token = ?, lease_generation = ?, "
                "lease_expires_at = ?, updated_at = ? WHERE project_id = ?",
                (token, generation, expires, timestamp, self.project_id),
            )
            _clean_staging(conn, self.project_id)
            ordinal = int(
                conn.execute(
                    "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM source_generations "
                    "WHERE project_id = ?",
                    (self.project_id,),
                ).fetchone()[0]
            )
            fingerprint_json = canonical_json(fingerprint)
            staging_id = new_id("gen")
            conn.execute(
                "INSERT INTO source_generations (id, project_id, ordinal, status, "
                "fingerprint_json, fingerprint_sha256, lease_generation, created_at) "
                "VALUES (?, ?, ?, 'staging', ?, ?, ?, ?)",
                (
                    staging_id,
                    self.project_id,
                    ordinal,
                    fingerprint_json,
                    sha256_hex(fingerprint_json),
                    generation,
                    timestamp,
                ),
            )
            return Lease(token, generation, int(row["revision"]), staging_id, ordinal)

        return self._database.write(write)

    def renew(self, lease: Lease) -> None:
        now = self._clock()
        expires = db_timestamp(now + timedelta(seconds=LEASE_SECONDS))

        def write(conn: sqlite3.Connection) -> None:
            _check_fence(conn, self.project_id, lease)
            conn.execute(
                "UPDATE source_catalogs SET lease_expires_at = ? WHERE project_id = ?",
                (expires, self.project_id),
            )

        self._database.write(write)

    def release(self, lease: Lease) -> None:
        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                "DELETE FROM source_generation_files WHERE generation_id IN (SELECT id FROM "
                "source_generations WHERE id = ? AND status = 'staging')",
                (lease.staging_id,),
            )
            conn.execute(
                "DELETE FROM source_generations WHERE id = ? AND status = 'staging'",
                (lease.staging_id,),
            )
            conn.execute(
                "UPDATE source_catalogs SET lease_token = NULL, lease_expires_at = NULL "
                "WHERE project_id = ? AND lease_token = ? AND lease_generation = ?",
                (self.project_id, lease.token, lease.generation),
            )

        self._database.write(write)

    # Leitura da geração ativa -------------------------------------------------------------

    def active(self) -> ActiveGeneration | None:
        return self._database.read(lambda conn: _active(conn, self.project_id))

    def active_files(self) -> dict[tuple[str, str], ActiveFile]:
        def read(conn: sqlite3.Connection) -> dict[tuple[str, str], ActiveFile]:
            generation = _active(conn, self.project_id)
            if generation is None:
                return {}
            rows = conn.execute(
                "SELECT f.*, d.file_sha256, d.byte_size FROM source_generation_files f "
                "LEFT JOIN source_documents d ON d.id = f.document_id "
                "WHERE f.generation_id = ?",
                (generation.id,),
            ).fetchall()
            return {
                (row["source_id"], row["relative_path"]): ActiveFile(
                    source_id=row["source_id"],
                    relative_path=row["relative_path"],
                    status=FileStatus(row["status"]),
                    reason=row["reason"],
                    document_id=row["document_id"],
                    file_sha256=row["file_sha256"],
                    byte_size=int(row["byte_size"] or 0),
                    modified_at=row["modified_at"],
                )
                for row in rows
            }

        return self._database.read(read)

    def find_document(
        self,
        source_id: str,
        relative_path: str,
        file_sha256: str,
        extraction: str,
        chunking: str,
        model: str,
    ) -> str | None:
        """Documento reaproveitável só se chunks e vetores da configuração atual existem."""

        def read(conn: sqlite3.Connection) -> str | None:
            row = conn.execute(
                "SELECT id FROM source_documents WHERE source_id = ? AND relative_path = ? "
                "AND file_sha256 = ? AND extraction_fingerprint = ?",
                (source_id, relative_path, file_sha256, extraction),
            ).fetchone()
            if row is None:
                return None
            missing = conn.execute(
                "SELECT COUNT(*) FROM source_chunks c WHERE c.document_id = ? "
                "AND c.chunking_fingerprint = ? AND NOT EXISTS (SELECT 1 FROM source_embeddings "
                "e WHERE e.chunk_id = c.id AND e.model_fingerprint = ?)",
                (row["id"], chunking, model),
            ).fetchone()[0]
            chunks = conn.execute(
                "SELECT COUNT(*) FROM source_chunks WHERE document_id = ? "
                "AND chunking_fingerprint = ?",
                (row["id"], chunking),
            ).fetchone()[0]
            return str(row["id"]) if chunks and not missing else None

        return self._database.read(read)

    def store_document(
        self,
        lease: Lease,
        draft: DocumentDraft,
        chunks: Sequence[ChunkDraft],
        vectors: Sequence[Sequence[bytes]],
        chunking: str,
        model: str,
    ) -> str:
        """Documento completo numa transação: nunca chunks antigos misturados a novos."""
        timestamp = self._now()

        def write(conn: sqlite3.Connection) -> str:
            _check_fence(conn, self.project_id, lease)
            row = conn.execute(
                "SELECT id FROM source_documents WHERE source_id = ? AND relative_path = ? "
                "AND file_sha256 = ? AND extraction_fingerprint = ?",
                (
                    draft.source_id,
                    draft.relative_path,
                    draft.file_sha256,
                    draft.extraction_fingerprint,
                ),
            ).fetchone()
            if row is None:
                document_id = new_id("doc")
                conn.execute(
                    "INSERT INTO source_documents (id, source_id, relative_path, format, "
                    "file_sha256, text_sha256, byte_size, extracted_characters, normalized_text, "
                    "extraction_fingerprint, declarations_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        document_id,
                        draft.source_id,
                        draft.relative_path,
                        draft.format,
                        draft.file_sha256,
                        draft.text_sha256,
                        draft.byte_size,
                        len(draft.normalized_text),
                        draft.normalized_text,
                        draft.extraction_fingerprint,
                        canonical_json(draft.declarations),
                        timestamp,
                    ),
                )
            else:
                document_id = str(row["id"])
            # Chunks são determinísticos por documento e chunking: num rebuild só de modelo, os
            # da geração ativa continuam servindo consultas e só ganham vetores novos.
            existing = {
                int(item["ordinal"]): str(item["id"])
                for item in conn.execute(
                    "SELECT id, ordinal FROM source_chunks WHERE document_id = ? "
                    "AND chunking_fingerprint = ?",
                    (document_id, chunking),
                )
            }
            if existing and sorted(existing) != [chunk.ordinal for chunk in chunks]:
                _delete_chunks(conn, list(existing.values()))
                existing = {}
            for chunk, windows in zip(chunks, vectors, strict=True):
                reused = existing.get(chunk.ordinal)
                if reused is not None:
                    for index, ((start, end), vector) in enumerate(
                        zip(chunk.windows, windows, strict=True)
                    ):
                        conn.execute(
                            "INSERT OR IGNORE INTO source_embeddings (chunk_id, "
                            "model_fingerprint, window_ordinal, token_start, token_end, "
                            "dimension, vector) VALUES (?, ?, ?, ?, ?, 1024, ?)",
                            (reused, model, index, start, end, vector),
                        )
                    continue
                chunk_id = new_id("chunk")
                conn.execute(
                    "INSERT INTO source_chunks (id, document_id, ordinal, heading, "
                    "locator_json, text, token_count, content_sha256, chunking_fingerprint) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        chunk_id,
                        document_id,
                        chunk.ordinal,
                        chunk.heading,
                        canonical_json(chunk.locator),
                        chunk.text,
                        chunk.token_count,
                        chunk.content_sha256,
                        chunking,
                    ),
                )
                conn.execute(
                    "INSERT INTO source_chunks_fts (chunk_id, heading, text) VALUES (?, ?, ?)",
                    (chunk_id, chunk.heading, chunk.text),
                )
                for index, ((start, end), vector) in enumerate(
                    zip(chunk.windows, windows, strict=True)
                ):
                    conn.execute(
                        "INSERT INTO source_embeddings (chunk_id, model_fingerprint, "
                        "window_ordinal, token_start, token_end, dimension, vector) "
                        "VALUES (?, ?, ?, ?, ?, 1024, ?)",
                        (chunk_id, model, index, start, end, vector),
                    )
            return document_id

        return self._database.write(write)

    def publish(
        self,
        lease: Lease,
        files: Sequence[FileRow],
        synced_sources: Iterable[str],
        hook: Publisher | None = None,
    ) -> int:
        """Ativa a geração preparada; revisão ou fencing divergentes recusam a publicação."""
        timestamp = self._now()
        synced = tuple(synced_sources)

        def write(conn: sqlite3.Connection) -> int:
            _check_fence(conn, self.project_id, lease)
            catalog = conn.execute(
                "SELECT revision, active_generation_id FROM source_catalogs WHERE project_id = ?",
                (self.project_id,),
            ).fetchone()
            if int(catalog["revision"]) != lease.revision:
                raise SourcesError(
                    CATALOG_REVISION_CONFLICT, "O catálogo mudou durante a sincronização."
                )
            for item in files:
                conn.execute(
                    "INSERT INTO source_generation_files (generation_id, source_id, "
                    "relative_path, document_id, modified_at, status, reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        lease.staging_id,
                        item.source_id,
                        item.relative_path,
                        item.document_id,
                        item.modified_at,
                        item.status.value,
                        item.reason,
                    ),
                )
            previous = catalog["active_generation_id"]
            conn.execute(
                "UPDATE source_generations SET status = 'retired' WHERE project_id = ? "
                "AND status = 'active'",
                (self.project_id,),
            )
            conn.execute(
                "UPDATE source_generations SET status = 'active', published_at = ? WHERE id = ?",
                (timestamp, lease.staging_id),
            )
            conn.execute(
                "UPDATE source_catalogs SET active_generation_id = ?, revision = revision + 1, "
                "updated_at = ? WHERE project_id = ?",
                (lease.staging_id, timestamp, self.project_id),
            )
            for source_id in synced:
                conn.execute(
                    "UPDATE document_sources SET last_sync_at = ? WHERE id = ? AND project_id = ?",
                    (timestamp, source_id, self.project_id),
                )
            if hook is not None:
                hook(conn, lease.staging_id, previous, timestamp)
            _collect_garbage(conn, self.project_id)
            return lease.staging_ordinal

        return self._database.write(write)

    def statuses(self) -> list[SourceStatus]:
        def read(conn: sqlite3.Connection) -> list[SourceStatus]:
            generation = _active(conn, self.project_id)
            result: list[SourceStatus] = []
            for row in conn.execute(
                "SELECT * FROM document_sources WHERE project_id = ? ORDER BY created_at, id",
                (self.project_id,),
            ).fetchall():
                counts = {"ready": 0, "excluded": 0, "failed": 0}
                chunks = 0
                if generation is not None:
                    for item in conn.execute(
                        "SELECT status, COUNT(*) AS total FROM source_generation_files "
                        "WHERE generation_id = ? AND source_id = ? GROUP BY status",
                        (generation.id, row["id"]),
                    ):
                        counts[str(item["status"])] = int(item["total"])
                    chunks = int(
                        conn.execute(
                            "SELECT COUNT(*) FROM source_chunks c JOIN source_generation_files f "
                            "ON f.document_id = c.document_id WHERE f.generation_id = ? "
                            "AND f.source_id = ? AND f.status = 'ready' "
                            "AND c.chunking_fingerprint = ?",
                            (generation.id, row["id"], _fp(generation, "chunking")),
                        ).fetchone()[0]
                    )
                result.append(
                    SourceStatus(
                        id=row["id"],
                        label=row["display_label"],
                        created_at=row["created_at"],
                        last_sync_at=row["last_sync_at"],
                        ready=counts["ready"],
                        excluded=counts["excluded"],
                        failed=counts["failed"],
                        active_chunks=chunks,
                    )
                )
            return result

        return self._database.read(read)

    def active_chunks(self) -> int:
        def read(conn: sqlite3.Connection) -> int:
            generation = _active(conn, self.project_id)
            if generation is None:
                return 0
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM source_chunks c JOIN source_generation_files f "
                    "ON f.document_id = c.document_id WHERE f.generation_id = ? "
                    "AND f.status = 'ready' AND c.chunking_fingerprint = ?",
                    (generation.id, _fp(generation, "chunking")),
                ).fetchone()[0]
            )

        return self._database.read(read)

    def has_active(self) -> bool:
        return self.active() is not None

    def now(self) -> str:
        return self._now()

    def transaction[T](self, operation: Callable[[sqlite3.Connection], T]) -> T:
        """Transação de escrita para os fluxos de disputa e proveniência das fontes."""
        return self._database.write(operation)

    def conflicts(self, run_id: str) -> list[SourceConflict]:
        return self._database.read(lambda conn: run_conflicts(conn, self.project_id, run_id))

    # Consulta -----------------------------------------------------------------------------

    def query(
        self,
        *,
        run_id: str | None,
        flow: str,
        query: str,
        vector: bytes,
        model: str,
        limit: int,
    ) -> QueryOutcome:
        """Busca e gravação das referências na mesma transação, sobre a mesma geração.

        Se a gravação falhar, nenhum texto é devolvido: toda passagem entregue é auditável.
        """
        timestamp = self._now()

        def write(conn: sqlite3.Connection) -> QueryOutcome:
            generation = _active(conn, self.project_id)
            if generation is None:
                return QueryOutcome(None, None, (), False, 0, NO_SOURCES)
            if _fp(generation, "model") != model:
                raise SourcesError(
                    INDEX_INCOMPATIBLE,
                    "O índice ativo foi gerado com outro modelo; rode lastro sources sync "
                    "--rebuild.",
                )
            chunking = _fp(generation, "chunking")
            lexical = _lexical(conn, generation.id, chunking, query)
            semantic = _semantic(conn, generation.id, chunking, model, vector)
            ranked = fuse(lexical, semantic, limit)
            if not ranked:
                return QueryOutcome(None, generation.ordinal, (), False, 0, NO_MATCHES)
            query_id = new_id("sq")
            drafts = [
                _passage(conn, self.project_id, candidate.chunk_id, rank)
                for rank, (candidate, _) in enumerate(ranked, start=1)
            ]
            included, omitted = fit_passages(drafts)
            conn.execute(
                "INSERT INTO source_queries (id, run_id, project_id, flow, generation_id, "
                "query_text, model_fingerprint, created_at, truncated, omitted_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    query_id,
                    run_id,
                    self.project_id,
                    flow,
                    generation.id,
                    redact_text(query),
                    model,
                    timestamp,
                    int(omitted > 0),
                    omitted,
                ),
            )
            delivered: list[Passage] = []
            for passage in included:
                reference_id = new_id("sref")
                snapshot = _snapshot(conn, passage, generation, flow, run_id, self.project_id)
                snapshot["reference_id"] = reference_id
                body = canonical_json(snapshot)
                conn.execute(
                    "INSERT INTO source_run_refs (id, query_id, run_id, chunk_identity, rank, "
                    "snapshot_json, snapshot_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        reference_id,
                        query_id,
                        run_id,
                        passage.chunk_id,
                        passage.rank,
                        body,
                        sha256_hex(body),
                        timestamp,
                    ),
                )
                delivered.append(
                    Passage(
                        reference_id=reference_id,
                        chunk_id=passage.chunk_id,
                        text=passage.text,
                        citation=passage.citation,
                        version=passage.version,
                        locator=passage.locator,
                        contested=passage.contested,
                        token_count=passage.token_count,
                        rank=passage.rank,
                    )
                )
            return QueryOutcome(
                query_id, generation.ordinal, tuple(delivered), omitted > 0, omitted
            )

        return self._retrieval().write(write)

    def run_reference(self, run_id: str, reference_id: str) -> JsonObject | None:
        """Snapshot de uma passagem entregue a esta run; outra run ou projeto não resolve."""

        def read(conn: sqlite3.Connection) -> JsonObject | None:
            row = conn.execute(
                "SELECT r.snapshot_json FROM source_run_refs r JOIN source_queries q "
                "ON q.id = r.query_id WHERE r.id = ? AND r.run_id = ? AND q.project_id = ?",
                (reference_id, run_id, self.project_id),
            ).fetchone()
            return _object(row["snapshot_json"]) if row is not None else None

        return self._database.read(read)

    def run_references(self, run_id: str) -> list[JsonObject]:
        def read(conn: sqlite3.Connection) -> list[JsonObject]:
            return [
                _object(row["snapshot_json"])
                for row in conn.execute(
                    "SELECT r.snapshot_json FROM source_run_refs r JOIN source_queries q "
                    "ON q.id = r.query_id WHERE r.run_id = ? AND q.project_id = ? "
                    "ORDER BY r.created_at, r.rank",
                    (run_id, self.project_id),
                )
            ]

        return self._database.read(read)


def _fp(generation: ActiveGeneration, key: str) -> str:
    return str(generation.fingerprint.get(key, ""))


def _active(conn: sqlite3.Connection, project_id: str) -> ActiveGeneration | None:
    row = conn.execute(
        "SELECT g.id, g.ordinal, g.fingerprint_json FROM source_catalogs c "
        "JOIN source_generations g ON g.id = c.active_generation_id WHERE c.project_id = ?",
        (project_id,),
    ).fetchone()
    if row is None:
        return None
    return ActiveGeneration(row["id"], int(row["ordinal"]), _object(row["fingerprint_json"]))


def _check_fence(conn: sqlite3.Connection, project_id: str, lease: Lease) -> None:
    row = conn.execute(
        "SELECT lease_token, lease_generation FROM source_catalogs WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    if (
        row is None
        or row["lease_token"] != lease.token
        or row["lease_generation"] != lease.generation
    ):
        raise SourcesError(
            CATALOG_REVISION_CONFLICT,
            "O lease desta sincronização expirou e outro escritor assumiu o catálogo.",
        )


def _clean_staging(conn: sqlite3.Connection, project_id: str) -> None:
    staging = [
        str(row["id"])
        for row in conn.execute(
            "SELECT id FROM source_generations WHERE project_id = ? AND status = 'staging'",
            (project_id,),
        )
    ]
    for generation_id in staging:
        conn.execute(
            "DELETE FROM source_generation_files WHERE generation_id = ?", (generation_id,)
        )
        conn.execute("DELETE FROM source_generations WHERE id = ?", (generation_id,))
    _collect_garbage(conn, project_id)


def _delete_chunks(conn: sqlite3.Connection, chunk_ids: Sequence[str]) -> None:
    for chunk_id in chunk_ids:
        conn.execute("DELETE FROM source_embeddings WHERE chunk_id = ?", (chunk_id,))
        conn.execute("DELETE FROM source_chunks_fts WHERE chunk_id = ?", (chunk_id,))
        conn.execute("DELETE FROM source_chunks WHERE id = ?", (chunk_id,))


def _delete_documents(conn: sqlite3.Connection, document_ids: Sequence[str]) -> None:
    for document_id in document_ids:
        _delete_chunks(
            conn,
            [
                str(row["id"])
                for row in conn.execute(
                    "SELECT id FROM source_chunks WHERE document_id = ?", (document_id,)
                )
            ],
        )
        conn.execute("DELETE FROM source_documents WHERE id = ?", (document_id,))


def _collect_garbage(conn: sqlite3.Connection, project_id: str) -> None:
    """Remove do índice o que nenhuma geração usa; snapshots citados não dependem disso.

    Só roda dentro do lease do próprio escritor (ao adquirir ou ao publicar), quando nenhum
    outro processo pode estar preparando documentos deste projeto.
    """
    conn.execute(
        "DELETE FROM source_generation_files WHERE generation_id IN (SELECT id FROM "
        "source_generations WHERE project_id = ? AND status = 'retired')",
        (project_id,),
    )
    orphans = [
        str(row["id"])
        for row in conn.execute(
            "SELECT d.id FROM source_documents d JOIN document_sources s ON s.id = d.source_id "
            "WHERE s.project_id = ? AND NOT EXISTS (SELECT 1 FROM source_generation_files f "
            "WHERE f.document_id = d.id)",
            (project_id,),
        )
    ]
    _delete_documents(conn, orphans)
    active = _active(conn, project_id)
    if active is None:
        return
    chunking = _fp(active, "chunking")
    model = _fp(active, "model")
    stale_chunks = [
        str(row["id"])
        for row in conn.execute(
            "SELECT c.id FROM source_chunks c JOIN source_generation_files f "
            "ON f.document_id = c.document_id WHERE f.generation_id = ? "
            "AND c.chunking_fingerprint <> ?",
            (active.id, chunking),
        )
    ]
    _delete_chunks(conn, stale_chunks)
    conn.execute(
        "DELETE FROM source_embeddings WHERE model_fingerprint <> ? AND chunk_id IN (SELECT c.id "
        "FROM source_chunks c JOIN source_generation_files f ON f.document_id = c.document_id "
        "WHERE f.generation_id = ?)",
        (model, active.id),
    )


def _lexical(
    conn: sqlite3.Connection, generation_id: str, chunking: str, query: str
) -> list[Candidate]:
    expression = match_expression(query)
    if expression is None:
        return []
    rows = conn.execute(
        "SELECT c.id, c.document_id, d.relative_path, c.ordinal FROM source_chunks_fts "
        "JOIN source_chunks c ON c.id = source_chunks_fts.chunk_id "
        "JOIN source_documents d ON d.id = c.document_id "
        "JOIN source_generation_files f ON f.document_id = d.id "
        "WHERE source_chunks_fts MATCH ? AND f.generation_id = ? AND f.status = 'ready' "
        "AND c.chunking_fingerprint = ? "
        "ORDER BY bm25(source_chunks_fts), d.relative_path, c.ordinal, c.id LIMIT ?",
        (expression, generation_id, chunking, CANDIDATES),
    ).fetchall()
    return [Candidate(row[0], row[1], row[2], int(row[3])) for row in rows]


def _semantic(
    conn: sqlite3.Connection, generation_id: str, chunking: str, model: str, vector: bytes
) -> list[Candidate]:
    try:
        rows = conn.execute(
            "SELECT c.id, c.document_id, d.relative_path, c.ordinal, "
            "MIN(vec_distance_cosine(e.vector, ?)) AS distance FROM source_embeddings e "
            "JOIN source_chunks c ON c.id = e.chunk_id "
            "JOIN source_documents d ON d.id = c.document_id "
            "JOIN source_generation_files f ON f.document_id = d.id "
            "WHERE f.generation_id = ? AND f.status = 'ready' AND c.chunking_fingerprint = ? "
            "AND e.model_fingerprint = ? GROUP BY c.id "
            "ORDER BY distance, d.relative_path, c.ordinal, c.id LIMIT ?",
            (vector, generation_id, chunking, model, CANDIDATES),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such function" in str(exc):
            raise SourcesError(
                SOURCES_UNAVAILABLE, "Busca vetorial indisponível; nenhum resultado parcial."
            ) from None
        raise
    return [Candidate(row[0], row[1], row[2], int(row[3])) for row in rows]


def _passage(conn: sqlite3.Connection, project_id: str, chunk_id: str, rank: int) -> Passage:
    row = conn.execute(
        "SELECT c.*, d.file_sha256, d.text_sha256, d.relative_path, d.source_id, "
        "s.display_label FROM source_chunks c JOIN source_documents d ON d.id = c.document_id "
        "JOIN document_sources s ON s.id = d.source_id WHERE c.id = ? AND s.project_id = ?",
        (chunk_id, project_id),
    ).fetchone()
    locator = _object(row["locator_json"])
    locator["path"] = f"{row['display_label']}/{row['relative_path']}"
    modified = conn.execute(
        "SELECT f.modified_at FROM source_generation_files f JOIN source_catalogs k "
        "ON k.active_generation_id = f.generation_id WHERE f.document_id = ? AND k.project_id = ?",
        (row["document_id"], project_id),
    ).fetchone()
    return Passage(
        reference_id="",
        chunk_id=chunk_id,
        text=row["text"],
        citation=citation(str(locator["path"]), str(row["heading"])),
        version={
            "file_sha256": row["file_sha256"],
            "text_sha256": row["text_sha256"],
            "modified_at": modified["modified_at"] if modified is not None else None,
        },
        locator=locator,
        contested=_contested(conn, project_id, chunk_id),
        token_count=int(row["token_count"]),
        rank=rank,
    )


def _contested(conn: sqlite3.Connection, project_id: str, chunk_id: str) -> bool:
    disputed = conn.execute(
        "SELECT 1 FROM source_conflicts WHERE project_id = ? AND status = 'open' AND ("
        "json_extract(left_snapshot_json, '$.chunk_id') = ? "
        "OR json_extract(right_snapshot_json, '$.chunk_id') = ?) LIMIT 1",
        (project_id, chunk_id, chunk_id),
    ).fetchone()
    if disputed is not None:
        return True
    memory = conn.execute(
        "SELECT 1 FROM source_knowledge_refs r JOIN knowledge k ON k.id = r.knowledge_id "
        "WHERE r.project_id = ? AND r.chunk_identity = ? AND r.active = 1 "
        "AND k.validation_state = 'contested' LIMIT 1",
        (project_id, chunk_id),
    ).fetchone()
    return memory is not None


def _snapshot(
    conn: sqlite3.Connection,
    passage: Passage,
    generation: ActiveGeneration,
    flow: str,
    run_id: str | None,
    project_id: str,
) -> JsonObject:
    row = conn.execute(
        "SELECT c.document_id, c.ordinal, c.heading, c.content_sha256, d.source_id, "
        "d.relative_path FROM source_chunks c JOIN source_documents d ON d.id = c.document_id "
        "WHERE c.id = ?",
        (passage.chunk_id,),
    ).fetchone()
    return {
        "kind": "passage",
        "source_type": "documentation",
        "authority": "declared",
        "project_id": project_id,
        "run_id": run_id,
        "flow": flow,
        "chunk_id": passage.chunk_id,
        "document_id": row["document_id"],
        "source_id": row["source_id"],
        "relative_path": row["relative_path"],
        "ordinal": int(row["ordinal"]),
        "heading": row["heading"],
        "content_sha256": row["content_sha256"],
        "citation": passage.citation,
        "text": passage.text,
        "version": passage.version,
        "locator": passage.locator,
        "rank": passage.rank,
        "contested": passage.contested,
        "generation_id": generation.id,
        "generation": generation.ordinal,
        "fingerprint": generation.fingerprint,
    }


# Disputas e proveniência documental -------------------------------------------------------


def open_disputes(conn: sqlite3.Connection, entry_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM source_conflict_entries e JOIN source_conflicts c ON c.id = e.conflict_id "
        "WHERE e.knowledge_id = ? AND c.status = 'open' LIMIT 1",
        (entry_id,),
    ).fetchone()
    return row is not None


def resolve_disputes(conn: sqlite3.Connection, entry_id: str, timestamp: str) -> tuple[str, ...]:
    """Decisão humana na memória fecha as disputas da entrada e devolve as outras afetadas."""
    conflicts = [
        str(row["conflict_id"])
        for row in conn.execute(
            "SELECT e.conflict_id FROM source_conflict_entries e JOIN source_conflicts c "
            "ON c.id = e.conflict_id WHERE e.knowledge_id = ? AND c.status = 'open'",
            (entry_id,),
        )
    ]
    partners: set[str] = set()
    for conflict_id in conflicts:
        conn.execute(
            "UPDATE source_conflicts SET status = 'resolved', resolved_at = ?, "
            "resolution_ref = ? WHERE id = ?",
            (timestamp, f"knowledge_mutation:{entry_id}", conflict_id),
        )
        partners.update(
            str(row["knowledge_id"])
            for row in conn.execute(
                "SELECT knowledge_id FROM source_conflict_entries WHERE conflict_id = ? "
                "AND knowledge_id <> ?",
                (conflict_id, entry_id),
            )
        )
    return tuple(sorted(partners))


def entry_snapshot(conn: sqlite3.Connection, entry_id: str) -> JsonObject:
    row = conn.execute(
        "SELECT id, revision, flow, statement, fact_json, value_sha256, source_type, source_ref, "
        "scope_json FROM knowledge WHERE id = ?",
        (entry_id,),
    ).fetchone()
    refs: list[JsonValue] = [
        _object(item["snapshot_json"])
        for item in conn.execute(
            "SELECT snapshot_json FROM source_knowledge_refs WHERE knowledge_id = ? "
            "ORDER BY created_at, id",
            (entry_id,),
        )
    ]
    return {
        "kind": "knowledge",
        "reference_id": row["id"],
        "revision": int(row["revision"]),
        "flow": row["flow"],
        "statement": row["statement"],
        "fact": _object(row["fact_json"]),
        "value_sha256": row["value_sha256"],
        "source_type": row["source_type"],
        "source_ref": row["source_ref"],
        "scope": _object(row["scope_json"]),
        "documentation": refs,
    }


def pair_hash(left: JsonObject, right: JsonObject) -> str:
    """Par canônico independente da ordem: tipo, referência e conteúdo de cada ponta."""

    def identity(side: JsonObject) -> str:
        # Passagem pelo chunk e conteúdo (não pelo ID da consulta); memória pelo valor, para
        # que contestar a entrada (nova revisão) não crie outra disputa para o mesmo par.
        kind = side.get("kind")
        if kind == "passage":
            key: list[JsonValue] = [kind, side.get("chunk_id"), side.get("content_sha256")]
        elif kind == "knowledge":
            key = [kind, side.get("reference_id"), side.get("value_sha256")]
        else:
            key = [kind, side.get("reference_id"), side.get("content_sha256")]
        return sha256_hex(canonical_json(key))

    ordered: list[JsonValue] = [*sorted([identity(left), identity(right)])]
    return sha256_hex(canonical_json(ordered))


def insert_conflict(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    run_id: str | None,
    flow: str,
    left: JsonObject,
    right: JsonObject,
    assessment: str,
    explanation: str,
    knowledge_ids: Sequence[str],
    timestamp: str,
) -> tuple[str, bool]:
    """Grava a disputa (ou devolve a já registrada para o mesmo par) com as entradas afetadas."""
    digest = pair_hash(left, right)
    existing = conn.execute(
        "SELECT id FROM source_conflicts WHERE project_id = ? AND flow = ? AND pair_sha256 = ?",
        (project_id, flow, digest),
    ).fetchone()
    if existing is not None:
        conflict_id = str(existing["id"])
        created = False
    else:
        conflict_id = new_id("sc")
        conn.execute(
            "INSERT INTO source_conflicts (id, project_id, run_id, flow, left_snapshot_json, "
            "right_snapshot_json, pair_sha256, assessment, explanation, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                conflict_id,
                project_id,
                run_id,
                flow,
                canonical_json(left),
                canonical_json(right),
                digest,
                assessment,
                redact_text(explanation),
                timestamp,
            ),
        )
        created = True
    for knowledge_id in knowledge_ids:
        conn.execute(
            "INSERT OR IGNORE INTO source_conflict_entries (conflict_id, knowledge_id) "
            "VALUES (?, ?)",
            (conflict_id, knowledge_id),
        )
    return conflict_id, created


def record_structured_conflict(
    conn: sqlite3.Connection,
    project_id: str,
    run_id: str | None,
    flow: str,
    document_entry: str,
    other_entry: str,
    timestamp: str,
) -> str | None:
    """Valores tipados divergentes com uma ponta documental viram disputa `structured`."""
    left = entry_snapshot(conn, document_entry)
    right = entry_snapshot(conn, other_entry)
    if DOCUMENTATION not in (left["source_type"], right["source_type"]):
        return None
    conflict_id, _ = insert_conflict(
        conn,
        project_id=project_id,
        run_id=run_id,
        flow=flow,
        left=left,
        right=right,
        assessment="structured",
        explanation="Valores tipados divergentes para o mesmo fato em escopos compatíveis.",
        knowledge_ids=(document_entry, other_entry),
        timestamp=timestamp,
    )
    return conflict_id


def conflict_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> SourceConflict:
    return SourceConflict(
        id=row["id"],
        flow=row["flow"],
        assessment=Assessment(row["assessment"]),
        status=row["status"],
        left=_object(row["left_snapshot_json"]),
        right=_object(row["right_snapshot_json"]),
        explanation=row["explanation"],
        run_id=row["run_id"],
        created_at=row["created_at"],
        knowledge_ids=tuple(
            str(item["knowledge_id"])
            for item in conn.execute(
                "SELECT knowledge_id FROM source_conflict_entries WHERE conflict_id = ? "
                "ORDER BY knowledge_id",
                (row["id"],),
            )
        ),
    )


def run_conflicts(conn: sqlite3.Connection, project_id: str, run_id: str) -> list[SourceConflict]:
    """Disputas registradas na run e as abertas que tocam o que a run consultou."""
    rows = conn.execute(
        "SELECT c.* FROM source_conflicts c WHERE c.project_id = ? AND (c.run_id = ? "
        "OR (c.status = 'open' AND EXISTS (SELECT 1 FROM source_conflict_entries e "
        "JOIN knowledge_run_refs r ON r.knowledge_id = e.knowledge_id "
        "WHERE e.conflict_id = c.id AND r.run_id = ?)) "
        "OR (c.status = 'open' AND EXISTS (SELECT 1 FROM source_run_refs s "
        "WHERE s.run_id = ? AND (s.chunk_identity = json_extract(c.left_snapshot_json, "
        "'$.chunk_id') OR s.chunk_identity = json_extract(c.right_snapshot_json, "
        "'$.chunk_id'))))) ORDER BY c.created_at, c.id",
        (project_id, run_id, run_id, run_id),
    ).fetchall()
    return [conflict_from_row(conn, row) for row in rows]


def link_knowledge(
    conn: sqlite3.Connection,
    project_id: str,
    knowledge_id: str,
    document_identity: str,
    chunk_id: str,
    snapshot: JsonObject,
    timestamp: str,
) -> None:
    body = canonical_json(snapshot)
    conn.execute(
        "INSERT INTO source_knowledge_refs (id, project_id, knowledge_id, "
        "source_document_identity, chunk_identity, source_type, snapshot_json, snapshot_sha256, "
        "active, created_at) VALUES (?, ?, ?, ?, ?, 'documentation', ?, ?, 1, ?) "
        "ON CONFLICT (knowledge_id, chunk_identity) DO UPDATE SET active = 1",
        (
            new_id("skr"),
            project_id,
            knowledge_id,
            document_identity,
            chunk_id,
            body,
            sha256_hex(body),
            timestamp,
        ),
    )


def select_run_references(
    conn: sqlite3.Connection, project_id: str, run_id: str
) -> list[JsonObject]:
    return [
        _object(row["snapshot_json"])
        for row in conn.execute(
            "SELECT r.snapshot_json FROM source_run_refs r JOIN source_queries q "
            "ON q.id = r.query_id WHERE r.run_id = ? AND q.project_id = ? "
            "ORDER BY r.created_at, r.rank, r.id",
            (run_id, project_id),
        )
    ]


def select_run_queries(conn: sqlite3.Connection, project_id: str, run_id: str) -> list[JsonObject]:
    """Metadados das consultas, sem o texto pedido pelo agente."""
    return [
        {
            "query_id": row["id"],
            "flow": row["flow"],
            "truncated": bool(row["truncated"]),
            "omitted_count": int(row["omitted_count"]),
            "created_at": row["created_at"],
        }
        for row in conn.execute(
            "SELECT id, flow, truncated, omitted_count, created_at FROM source_queries "
            "WHERE run_id = ? AND project_id = ? ORDER BY created_at, id",
            (run_id, project_id),
        )
    ]
