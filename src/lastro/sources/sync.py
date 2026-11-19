"""Cadastro, sincronização incremental e consulta das fontes documentais de um projeto.

Inventário, leitura, extração, scanner e embeddings acontecem fora de transações; o catálogo só
muda numa publicação curta e protegida pelo lease. Remoção só é inferida de inventário
completo, e qualquer falha antes da publicação mantém a geração anterior intacta.
"""

import hashlib
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from lastro.history.database import db_timestamp
from lastro.history.redaction import redact_text
from lastro.history.sources import (
    ActiveFile,
    DocumentDraft,
    FileRow,
    Lease,
    Publisher,
    SourceRepository,
)
from lastro.knowledge.models import JsonObject, JsonValue
from lastro.sources.chunking import chunk_document, chunking_fingerprint
from lastro.sources.embeddings import EmbeddingModel, require_extra
from lastro.sources.extraction import PdfRunner, extract, extraction_fingerprint, run_pdf_worker
from lastro.sources.models import (
    CORPUS_LIMIT_EXCEEDED,
    INVALID_SOURCES_INPUT,
    LEASE_RENEW_SECONDS,
    MAX_DECLARATIONS,
    POSSIBLE_SECRET,
    QUERY_MAX_TOKENS,
    SOURCE_UNAVAILABLE,
    ExtractionFailure,
    FileCounts,
    FileStatus,
    QueryOutcome,
    SourcesError,
    SourceStatus,
    SyncResult,
)
from lastro.sources.safety import (
    Candidate,
    DocumentReader,
    FileRejected,
    Inventory,
    ScannerFactory,
    SecretScanner,
    canonical_root,
    check_limits,
    flag_secrets,
    gitleaks_scanner,
    inventory,
    overlaps,
    root_key,
)

type Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _mtime(ns: int) -> str:
    return db_timestamp(datetime.fromtimestamp(ns / 1_000_000_000, UTC))


@dataclass
class _Plan:
    files: list[FileRow] = field(default_factory=list[FileRow])
    counts: FileCounts = field(default_factory=FileCounts)
    reasons: Counter[str] = field(default_factory=Counter[str])
    warnings: list[str] = field(default_factory=list[str])


class SourceService:
    """Operações humanas (add, sync, list) e a consulta usada pela tool do agente."""

    def __init__(
        self,
        repository: SourceRepository,
        *,
        project_names: tuple[str, ...],
        model_factory: Callable[[], EmbeddingModel],
        scanner_factory: ScannerFactory = gitleaks_scanner,
        pdf: PdfRunner = run_pdf_worker,
        clock: Clock = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        publish_hook: Publisher | None = None,
    ) -> None:
        self.repository = repository
        self.project_names = project_names
        self._model_factory = model_factory
        self._model: EmbeddingModel | None = None
        self._scanner_factory = scanner_factory
        self._pdf = pdf
        self._clock = clock
        self._monotonic = monotonic
        self.publish_hook = publish_hook

    @property
    def model(self) -> EmbeddingModel:
        if self._model is None:
            self._model = self._model_factory()
        return self._model

    def fingerprint(self) -> JsonObject:
        model = self.model
        return {
            "extraction": extraction_fingerprint(),
            "chunking": chunking_fingerprint(model.tokenizer),
            "model": model.fingerprint,
            "model_manifest": model.manifest,
        }

    # Cadastro -----------------------------------------------------------------------------

    def add(self, path: Path, project: str) -> SyncResult:
        """Cadastra a raiz e publica a primeira geração; repetir a raiz só sincroniza."""
        if project not in self.project_names:
            raise SourcesError(
                INVALID_SOURCES_INPUT,
                "O projeto informado não é o projeto configurado neste lastro.toml.",
            )
        root = canonical_root(path)
        key = root_key(root)
        label = root.name or "docs"
        for existing in self.repository.roots():
            if existing.canonical_root == str(root):
                continue
            if overlaps(Path(existing.canonical_root), root):
                raise SourcesError(
                    INVALID_SOURCES_INPUT,
                    f"A raiz se sobrepõe à fonte '{existing.display_label}' já cadastrada.",
                )
        require_extra()
        self.model  # noqa: B018
        scanner = self._scanner_factory()
        registered, created = self.repository.register_root(str(root), key, label)
        try:
            return self._sync(scanner, (registered.id,), rebuild=False)
        except BaseException:
            if created:
                self.repository.discard_root(registered.id)
            raise

    def sync(self, source_id: str | None = None, rebuild: bool = False) -> SyncResult:
        roots = self.repository.roots()
        if not roots:
            raise SourcesError(
                INVALID_SOURCES_INPUT, "Nenhuma fonte cadastrada. Use lastro sources add."
            )
        if source_id is not None and source_id not in {root.id for root in roots}:
            raise SourcesError(INVALID_SOURCES_INPUT, "source-id não pertence a este projeto.")
        require_extra()
        self.model  # noqa: B018
        scanner = self._scanner_factory()
        selected = (source_id,) if source_id is not None else tuple(root.id for root in roots)
        return self._sync(scanner, selected, rebuild=rebuild)

    def list(self) -> list[SourceStatus]:
        return self.repository.statuses()

    # Sincronização ------------------------------------------------------------------------

    def _sync(
        self, scanner: SecretScanner, selected: tuple[str, ...], *, rebuild: bool
    ) -> SyncResult:
        started = self._monotonic()
        fingerprint = self.fingerprint()
        lease = self.repository.acquire(uuid4().hex, fingerprint)
        renewed = self._monotonic()
        try:
            roots = {root.id: root for root in self.repository.roots()}
            previous = self.repository.active_files()
            active = self.repository.active()
            changed_config = active is None or any(
                active.fingerprint.get(key) != fingerprint[key]
                for key in ("extraction", "chunking", "model")
            )
            listings: dict[str, Inventory] = {}
            for source_id in selected:
                root = Path(roots[source_id].canonical_root)
                try:
                    current = canonical_root(root)
                except SourcesError:
                    raise SourcesError(
                        SOURCE_UNAVAILABLE,
                        f"A fonte '{roots[source_id].display_label}' está inacessível; nada foi "
                        "removido do índice.",
                    ) from None
                if str(current) != roots[source_id].canonical_root:
                    raise SourcesError(
                        SOURCE_UNAVAILABLE,
                        f"A raiz da fonte '{roots[source_id].display_label}' mudou de lugar.",
                    )
                listings[source_id] = inventory(current)
            kept = [item for item in previous.values() if item.source_id not in selected]
            total_files = sum(len(item.candidates) for item in listings.values()) + sum(
                1 for item in kept if item.status != FileStatus.EXCLUDED
            )
            total_bytes = sum(item.total_bytes for item in listings.values()) + sum(
                item.byte_size for item in kept if item.status == FileStatus.READY
            )
            problem = check_limits(total_files, total_bytes)
            if problem is not None:
                raise SourcesError(
                    CORPUS_LIMIT_EXCEEDED, problem + "; a geração ativa foi preservada."
                )
            plan = _Plan()
            for item in kept:
                plan.files.append(
                    FileRow(
                        item.source_id,
                        item.relative_path,
                        item.status,
                        item.document_id,
                        item.modified_at,
                        item.reason,
                    )
                )
            for source_id, listing in listings.items():
                reader = DocumentReader(Path(roots[source_id].canonical_root))
                seen: set[str] = set()
                for exclusion in listing.exclusions:
                    seen.add(exclusion.relative_path)
                    self._exclude(plan, source_id, exclusion.relative_path, exclusion.reason, None)
                for candidate in listing.candidates:
                    seen.add(candidate.relative_path)
                    before = previous.get((source_id, candidate.relative_path))
                    self._process(
                        plan,
                        lease,
                        scanner,
                        reader,
                        source_id,
                        candidate,
                        before,
                        rebuild or changed_config,
                    )
                    if self._monotonic() - renewed >= LEASE_RENEW_SECONDS:
                        self.repository.renew(lease)
                        renewed = self._monotonic()
                for owner, relative in previous:
                    if owner == source_id and relative not in seen:
                        plan.counts.removed += 1
            generation = self.repository.publish(lease, plan.files, selected, self.publish_hook)
        finally:
            self.repository.release(lease)
        partial = plan.counts.failed > 0 or plan.counts.excluded > 0
        if plan.counts.failed:
            plan.warnings.append(
                f"{plan.counts.failed} arquivo(s) indisponível(is) por falha de extração; "
                "suas versões anteriores saíram da busca."
            )
        if plan.counts.excluded:
            plan.warnings.append(
                f"{plan.counts.excluded} arquivo(s) excluído(s) por segurança; nada deles foi "
                "indexado."
            )
        return SyncResult(
            status="partial" if partial else "ok",
            project=self.project_names[-1],
            source_ids=selected,
            generation=generation,
            files=plan.counts,
            active_chunks=self.repository.active_chunks(),
            duration_seconds=self._monotonic() - started,
            reasons=dict(plan.reasons),
            warnings=tuple(plan.warnings),
        )

    def _exclude(
        self, plan: _Plan, source_id: str, relative: str, reason: str, modified: str | None
    ) -> None:
        plan.counts.excluded += 1
        plan.reasons[reason] += 1
        plan.files.append(FileRow(source_id, relative, FileStatus.EXCLUDED, None, modified, reason))

    def _fail(
        self, plan: _Plan, source_id: str, relative: str, reason: str, modified: str | None
    ) -> None:
        plan.counts.failed += 1
        plan.reasons[reason] += 1
        plan.files.append(FileRow(source_id, relative, FileStatus.FAILED, None, modified, reason))

    def _process(
        self,
        plan: _Plan,
        lease: Lease,
        scanner: SecretScanner,
        reader: DocumentReader,
        source_id: str,
        candidate: Candidate,
        before: ActiveFile | None,
        rebuild: bool,
    ) -> None:
        relative = candidate.relative_path
        modified = _mtime(candidate.mtime_ns)
        try:
            data = reader.read(candidate)
        except FileRejected as rejected:
            self._exclude(plan, source_id, relative, rejected.reason, modified)
            return
        # O hash bruto só é usado depois do scanner aprovar; até lá fica só em memória.
        file_sha256 = hashlib.sha256(data).hexdigest()
        model = self.model
        extraction = extraction_fingerprint()
        chunking = chunking_fingerprint(model.tokenizer)
        document_id = self.repository.find_document(
            source_id, relative, file_sha256, extraction, chunking, model.fingerprint
        )
        if document_id is None or rebuild:
            try:
                document = extract(candidate.format, data, relative, self._pdf)
            except ExtractionFailure as failure:
                self._fail(plan, source_id, relative, failure.reason, modified)
                return
            scanned = [document.text, *(item.body for item in document.declarations)]
            if any(flag_secrets(scanner, scanned)):
                self._exclude(plan, source_id, relative, POSSIBLE_SECRET, modified)
                return
            chunks = chunk_document(document, relative, model.tokenizer)
            if not chunks:
                self._fail(plan, source_id, relative, "empty_document", modified)
                return
            texts = [text for chunk in chunks for text in chunk.window_texts]
            embedded = model.embed_passages(texts)
            vectors: list[list[bytes]] = []
            cursor = 0
            for chunk in chunks:
                vectors.append(embedded[cursor : cursor + len(chunk.windows)])
                cursor += len(chunk.windows)
            declarations: list[JsonValue] = [
                {"heading": item.heading, "body": item.body, "line": item.line}
                for item in document.declarations[:MAX_DECLARATIONS]
            ]
            if len(document.declarations) > MAX_DECLARATIONS:
                plan.warnings.append(
                    redact_text(
                        f"{relative}: mais de {MAX_DECLARATIONS} declarações; excedentes ignoradas."
                    )
                )
            document_id = self.repository.store_document(
                lease,
                DocumentDraft(
                    source_id=source_id,
                    relative_path=relative,
                    format=candidate.format.value,
                    file_sha256=file_sha256,
                    text_sha256=hashlib.sha256(document.text.encode("utf-8")).hexdigest(),
                    byte_size=len(data),
                    normalized_text=document.text,
                    extraction_fingerprint=extraction,
                    declarations=declarations,
                ),
                chunks,
                vectors,
                chunking,
                model.fingerprint,
            )
        if before is None:
            plan.counts.added += 1
        elif before.document_id == document_id:
            plan.counts.unchanged += 1
        else:
            plan.counts.updated += 1
        plan.files.append(
            FileRow(source_id, relative, FileStatus.READY, document_id, modified, None)
        )

    # Consulta -----------------------------------------------------------------------------

    def query(self, *, run_id: str | None, flow: str, text: str, limit: int) -> QueryOutcome:
        """Recupera só da geração ativa do projeto; o modelo nunca é baixado aqui."""
        if not self.repository.has_active():
            return QueryOutcome(None, None, (), False, 0, "no_sources")
        model = self.model
        if len(model.tokenizer.spans(text)) > QUERY_MAX_TOKENS:
            raise SourcesError(
                INVALID_SOURCES_INPUT, f"A consulta excede {QUERY_MAX_TOKENS} tokens."
            )
        vector = model.embed_query(text)
        return self.repository.query(
            run_id=run_id,
            flow=flow,
            query=text,
            vector=vector,
            model=model.fingerprint,
            limit=limit,
        )
