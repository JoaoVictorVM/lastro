"""Contratos das fontes documentais: limites, erros, DTOs e entradas das tools MCP.

Documentação é fonte declarada; nada aqui a promove a fato observado nem a confirmação humana.
"""

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from lastro.knowledge.models import Category, Fact, JsonObject, Key

MAX_FILES = 2000
MAX_BYTES = 200_000_000
EXTENSIONS: dict[str, str] = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "text",
    ".html": "html",
    ".htm": "html",
    ".pdf": "pdf",
}

CHUNK_TOKENS = 800
CHUNK_OVERLAP = 100
WINDOW_TOKENS = 480
WINDOW_OVERLAP = 64
WINDOW_LIMIT = 512
QUERY_MAX_TOKENS = 200
QUERY_MAX_CHARACTERS = 1000

TOP_K = 5
CANDIDATES = 20
RRF_K = 60
RESPONSE_TOKENS = 4000
RESPONSE_CHARACTERS = 48_000

EXTRACTION_TIMEOUT_SECONDS = 60.0
EXTRACTION_MEMORY_BYTES = 512 * 1024 * 1024
EXTRACTED_CHARACTERS = 10_000_000

LEASE_SECONDS = 120
LEASE_RENEW_SECONDS = 30

MODEL_NAME = "intfloat/multilingual-e5-large"
DIMENSION = 1024
EMBEDDING_BATCH = 8
QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "

MAX_DECLARATIONS = 100
MAX_DECLARATION_CHARACTERS = 4000
FACT_FENCE = "lastro-fact"

NORMALIZATION_VERSION = "1"
CHUNKING_VERSION = "1"
EXTRACTOR_VERSION = "1"

MAX_QUOTE = 500
MAX_EXPLANATION = 1000
MAX_IDEMPOTENCY_KEY = 100

# Erros de domínio estáveis; mensagens são diagnósticas e nunca carregam texto de documento.
SOURCES_UNAVAILABLE = "sources_unavailable"
MODEL_NOT_READY = "model_not_ready"
INVALID_SOURCES_INPUT = "invalid_sources_input"
CORPUS_LIMIT_EXCEEDED = "corpus_limit_exceeded"
SOURCE_UNAVAILABLE = "source_unavailable"
SOURCES_BUSY = "sources_busy"
CATALOG_REVISION_CONFLICT = "catalog_revision_conflict"
INDEX_INCOMPATIBLE = "index_incompatible"
INVALID_SOURCE_REFERENCE = "invalid_source_reference"
SCOPE_MISMATCH = "scope_mismatch"
SCANNER_UNAVAILABLE = "scanner_unavailable"
IDEMPOTENCY_CONFLICT = "idempotency_conflict"

NO_SOURCES = "no_sources"
NO_MATCHES = "no_matches"

# Motivos por arquivo.
UNSAFE_PATH = "unsafe_path"
DENIED_PATTERN = "denied_pattern"
POSSIBLE_SECRET = "possible_secret"
UNSTABLE_FILE = "unstable_file"
NO_TEXT_LAYER = "no_text_layer"
ENCRYPTED_DOCUMENT = "encrypted_document"
EXTRACTION_FAILED = "extraction_failed"
EXTRACTION_TIMEOUT = "extraction_timeout"
RESOURCE_LIMIT = "resource_limit"
UNSUPPORTED_ENCODING = "unsupported_encoding"
EMPTY_DOCUMENT = "empty_document"
FACT_METADATA_INVALID = "fact_metadata_invalid"

EXCLUSION_REASONS = frozenset({UNSAFE_PATH, DENIED_PATTERN, POSSIBLE_SECRET, UNSTABLE_FILE})
FAILURE_REASONS = frozenset(
    {
        NO_TEXT_LAYER,
        ENCRYPTED_DOCUMENT,
        EXTRACTION_FAILED,
        EXTRACTION_TIMEOUT,
        RESOURCE_LIMIT,
        UNSUPPORTED_ENCODING,
        EMPTY_DOCUMENT,
    }
)

SOURCE_REF_PATTERN = re.compile(r"^sref_[0-9a-f]{32}$")


class SourcesError(Exception):
    """Erro de domínio das fontes; o código é contrato com CLI e MCP."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    def as_json(self) -> JsonObject:
        return {"status": "error", "code": self.code, "message": self.message}


class DocumentFormat(StrEnum):
    MARKDOWN = "markdown"
    TEXT = "text"
    HTML = "html"
    PDF = "pdf"


class FileStatus(StrEnum):
    READY = "ready"
    EXCLUDED = "excluded"
    FAILED = "failed"


class GenerationStatus(StrEnum):
    STAGING = "staging"
    ACTIVE = "active"
    RETIRED = "retired"


class Assessment(StrEnum):
    STRUCTURED = "structured"
    REPORTED = "reported"


class ReferenceKind(StrEnum):
    PASSAGE = "passage"
    KNOWLEDGE = "knowledge"
    CRITERION = "criterion"
    CLARIFICATION = "clarification"
    OBSERVATION = "observation"
    CONTROLLED_FILE = "controlled_file"


def document_format(relative: str) -> DocumentFormat | None:
    """Extensão elegível sem diferenciar caixa; demais arquivos não são candidatos."""
    name = relative.rsplit("/", 1)[-1].lower()
    dot = name.rfind(".")
    if dot <= 0:
        return None
    kind = EXTENSIONS.get(name[dot:])
    return DocumentFormat(kind) if kind is not None else None


# Extração ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Section:
    """Trecho contíguo do texto normalizado sob um heading e, no PDF, uma única página."""

    heading: str
    text: str
    start: int
    line_start: int | None = None
    page: int | None = None


@dataclass(frozen=True)
class Declaration:
    """Bloco `lastro-fact` cru, associado à seção onde aparece; validado só no adaptador."""

    heading: str
    body: str
    line: int | None


@dataclass(frozen=True)
class Extracted:
    text: str
    sections: tuple[Section, ...]
    declarations: tuple[Declaration, ...] = ()


class ExtractionFailure(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# Chunks -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkDraft:
    ordinal: int
    heading: str
    text: str
    start: int
    end: int
    token_count: int
    locator: JsonObject
    content_sha256: str
    windows: tuple[tuple[int, int], ...]
    window_texts: tuple[str, ...]


# Sincronização ----------------------------------------------------------------------------


@dataclass
class FileCounts:
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    excluded: int = 0
    failed: int = 0

    def as_json(self) -> JsonObject:
        return {
            "added": self.added,
            "updated": self.updated,
            "removed": self.removed,
            "unchanged": self.unchanged,
            "excluded": self.excluded,
            "failed": self.failed,
        }


@dataclass(frozen=True)
class SourceRoot:
    id: str
    project_id: str
    display_label: str
    created_at: str
    last_sync_at: str | None
    canonical_root: str = field(repr=False)


@dataclass(frozen=True)
class SyncResult:
    status: Literal["ok", "partial"]
    project: str
    source_ids: tuple[str, ...]
    generation: int
    files: FileCounts
    active_chunks: int
    duration_seconds: float
    reasons: dict[str, int]
    warnings: tuple[str, ...]

    def as_json(self) -> JsonObject:
        data: JsonObject = {
            "status": self.status,
            "project": self.project,
            "generation": self.generation,
            "files": self.files.as_json(),
            "active_chunks": self.active_chunks,
            "duration_seconds": round(self.duration_seconds, 3),
            "reasons": dict(sorted(self.reasons.items())),
            "warnings": list(self.warnings),
        }
        if len(self.source_ids) == 1:
            data["source_id"] = self.source_ids[0]
        else:
            data["source_ids"] = list(self.source_ids)
        return data


@dataclass(frozen=True)
class SourceStatus:
    id: str
    label: str
    created_at: str
    last_sync_at: str | None
    ready: int
    excluded: int
    failed: int
    active_chunks: int

    def as_json(self) -> JsonObject:
        return {
            "source_id": self.id,
            "label": self.label,
            "created_at": self.created_at,
            "last_sync_at": self.last_sync_at,
            "files": {"ready": self.ready, "excluded": self.excluded, "failed": self.failed},
            "active_chunks": self.active_chunks,
        }


# Consulta ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    chunk_id: str
    document_id: str
    relative_path: str
    ordinal: int


@dataclass(frozen=True)
class Passage:
    reference_id: str
    chunk_id: str
    text: str
    citation: str
    version: JsonObject
    locator: JsonObject
    contested: bool
    token_count: int
    rank: int

    def as_json(self) -> JsonObject:
        return {
            "reference_id": self.reference_id,
            "chunk_id": self.chunk_id,
            "source_type": "documentation",
            "authority": "declared",
            "text": self.text,
            "citation": self.citation,
            "version": self.version,
            "locator": self.locator,
            "contested": self.contested,
            "deployment_match": "unknown",
        }


@dataclass(frozen=True)
class QueryOutcome:
    query_id: str | None
    generation: int | None
    passages: tuple[Passage, ...]
    truncated: bool
    omitted_count: int
    reason: str | None = None

    def as_json(self) -> JsonObject:
        data: JsonObject = {
            "query_id": self.query_id,
            "generation": self.generation,
            "mode": "hybrid",
            "passages": [passage.as_json() for passage in self.passages],
            "truncated": self.truncated,
            "omitted_count": self.omitted_count,
        }
        if self.reason is not None:
            data["reason"] = self.reason
        return data


def citation(path: str, heading: str) -> str:
    return f"[{path} § {heading}]"


# Entradas MCP -----------------------------------------------------------------------------


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


NonBlank = AfterValidator(_not_blank)


class SourcesQueryInput(Strict):
    flow: Key
    query: Annotated[str, Field(min_length=1, max_length=QUERY_MAX_CHARACTERS), NonBlank]
    limit: int = Field(default=TOP_K, ge=1, le=TOP_K)


class ConflictSide(Strict):
    kind: ReferenceKind = Field(strict=False)
    reference_id: str = Field(min_length=1, max_length=200)
    quote: Annotated[str, Field(min_length=1, max_length=MAX_QUOTE), NonBlank]


class ConflictReportInput(Strict):
    flow: Key
    left: ConflictSide
    right: ConflictSide
    explanation: Annotated[str, Field(min_length=1, max_length=MAX_EXPLANATION), NonBlank]
    idempotency_key: str = Field(min_length=1, max_length=MAX_IDEMPOTENCY_KEY)

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        if (self.left.kind, self.left.reference_id) == (self.right.kind, self.right.reference_id):
            raise ValueError("left and right must reference different sources")
        if ReferenceKind.PASSAGE not in (self.left.kind, self.right.kind):
            raise ValueError("at least one side must be a documentation passage")
        return self


class DeclaredFact(Strict):
    """Corpo de um bloco `lastro-fact`: somente fluxo, categoria e fato já suportados."""

    flow: Key
    category: Category = Field(strict=False)
    fact: Fact


@dataclass(frozen=True)
class ConflictReceipt:
    conflict_id: str
    status: str
    assessment: Assessment
    contested_knowledge_ids: tuple[str, ...]
    deduplicated: bool

    def as_json(self) -> JsonObject:
        return {
            "conflict_id": self.conflict_id,
            "status": self.status,
            "assessment": self.assessment.value,
            "contested_knowledge_ids": list(self.contested_knowledge_ids),
            "deduplicated": self.deduplicated,
        }


@dataclass(frozen=True)
class SourceConflict:
    id: str
    flow: str
    assessment: Assessment
    status: str
    left: JsonObject
    right: JsonObject
    explanation: str
    run_id: str | None
    created_at: str
    knowledge_ids: tuple[str, ...]
