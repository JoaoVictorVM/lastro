"""Contratos do contexto de mudança: snapshot de issue, coleta Git e conjunto controlado.

Todo texto aqui já passou pela sanitização; hashes identificam o conteúdo sanitizado, nunca o
bruto. Mudanças apenas em valores redigidos podem produzir o mesmo hash.
"""

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, cast

from pydantic import BaseModel, ConfigDict, Field

# Mesmos aliases do histórico, definidos aqui para que o histórico possa importar este módulo.
type JsonValue = dict[str, JsonValue] | list[JsonValue] | str | int | float | bool | None
type JsonObject = dict[str, JsonValue]

MAX_ISSUE_JSON_BYTES = 1_000_000
MAX_ISSUE_BODY = 100_000
MAX_COMMENT_BODY = 10_000
MAX_CAPTURE_CHARACTERS = 500_000
MAX_COMMENTS = 50
MAX_FLOW_KEY = 200
CHANGE_CONTEXT_SCHEMA_VERSION = 1


class SourceKind(StrEnum):
    GITHUB = "github"
    MARKDOWN = "markdown"
    TEXT = "text"


class ScanLevel(StrEnum):
    GITLEAKS = "gitleaks"
    REDACTION_ONLY = "redaction_only"


class ComparisonMode(StrEnum):
    FEATURE = "feature"
    WORKING_TREE = "working-tree"
    PR = "pr"
    LAST_PASSED = "last-passed"


class SourceState(StrEnum):
    DISABLED = "disabled"
    AVAILABLE = "available"
    SKIPPED = "skipped"
    UNAVAILABLE = "unavailable"
    PARTIAL = "partial"


class Layer(StrEnum):
    COMMITTED = "committed"
    STAGED = "staged"
    UNSTAGED = "unstaged"
    UNTRACKED = "untracked"


class FileOrigin(StrEnum):
    COMMIT = "commit"
    INDEX = "index"
    WORKTREE = "worktree"
    BASE = "base"


class RemoteFreshness(StrEnum):
    NOT_CHECKED = "not_checked"
    FETCHED = "fetched"
    FETCH_FAILED = "fetch_failed"


class VersionMatch(StrEnum):
    MATCHED = "matched"
    MISMATCHED = "mismatched"
    UNKNOWN = "unknown"
    DIRTY_UNVERIFIED = "dirty_unverified"


EXCLUDED_SECRET = "excluded (possible secret)"


def canonical(value: JsonValue) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IssueComment(_Frozen):
    source_id: str | None = None
    body: Annotated[str, Field(max_length=MAX_COMMENT_BODY)]
    created_at: str | None = None
    updated_at: str | None = None


class IssueContent(_Frozen):
    """Conteúdo canônico sanitizado; os instantes locais de captura ficam fora do hash."""

    source_kind: SourceKind
    repository: str | None = None
    number: Annotated[int, Field(gt=0)] | None = None
    url: str | None = None
    title: Annotated[str, Field(min_length=1, max_length=1000)]
    body: Annotated[str, Field(max_length=MAX_ISSUE_BODY)]
    labels: tuple[str, ...] = ()
    comments: tuple[IssueComment, ...] = ()
    comments_total: int = Field(default=0, ge=0)
    comments_included: int = Field(default=0, ge=0)
    comments_truncated: bool = False
    source_updated_at: str | None = None

    def canonical_payload(self) -> JsonObject:
        return cast(JsonObject, self.model_dump(mode="json"))

    @property
    def content_sha256(self) -> str:
        return sha256_text(canonical(self.canonical_payload()))

    @property
    def source_identity(self) -> str:
        # Offline não inventa origem remota: a identidade é o tipo, isolado por projeto na tabela.
        if self.source_kind == SourceKind.GITHUB:
            return f"github:{self.repository}#{self.number}"
        return self.source_kind.value


def issue_requirements(content: IssueContent) -> str:
    """Projeção legível dos requisitos, estável para paginação por cursor."""
    lines = [f"# {content.title}", ""]
    if content.repository is not None and content.number is not None:
        lines += [f"Source: {content.repository}#{content.number}", ""]
    if content.labels:
        lines += [f"Labels: {', '.join(content.labels)}", ""]
    lines.append(content.body.strip() or "(empty body)")
    if content.comments:
        lines += ["", f"## Comments ({content.comments_included} of {content.comments_total})"]
        for index, comment in enumerate(content.comments, start=1):
            stamp = f" ({comment.created_at})" if comment.created_at else ""
            lines += ["", f"### Comment {index}{stamp}", comment.body.strip()]
    if content.comments_truncated:
        lines += ["", f"[{content.comments_total - content.comments_included} comment(s) omitted]"]
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class IssueCapture:
    content: IssueContent
    capture_started_at: str
    capture_finished_at: str
    scan_level: ScanLevel

    def stored_payload(self) -> JsonObject:
        payload = self.content.canonical_payload()
        payload["capture_started_at"] = self.capture_started_at
        payload["capture_finished_at"] = self.capture_finished_at
        payload["scan_level"] = self.scan_level.value
        return payload


@dataclass(frozen=True)
class ChangedFile:
    path: str
    layer: Layer
    status: str
    file_id: str | None = None
    excluded_reason: str | None = None

    def as_json(self) -> JsonObject:
        data: JsonObject = {"path": self.path, "layer": self.layer.value, "status": self.status}
        if self.file_id is not None:
            data["file_id"] = self.file_id
        if self.excluded_reason is not None:
            data["excluded_reason"] = self.excluded_reason
        return data


@dataclass(frozen=True)
class Exclusion:
    path: str
    reason: str

    def as_json(self) -> JsonObject:
        return {"path": self.path, "reason": self.reason}


@dataclass(frozen=True)
class SourceFile:
    """Conteúdo aprovado e congelado; nunca relido do repositório para servir a run."""

    file_id: str
    relative_path: str
    origin: FileOrigin
    revision_oid: str | None
    content: str

    @property
    def data(self) -> bytes:
        return self.content.encode("utf-8")

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def size_bytes(self) -> int:
        return len(self.data)

    def as_json(self) -> JsonObject:
        return {
            "file_id": self.file_id,
            "path": self.relative_path,
            "origin": self.origin.value,
            "revision_oid": self.revision_oid,
            "size_bytes": self.size_bytes,
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True)
class SecurityNotice:
    reason: str
    reference: str

    def as_json(self) -> JsonObject:
        return {"category": "source_boundary", "reason": self.reason, "reference": self.reference}


@dataclass(frozen=True)
class ChangeContextDraft:
    """Captura congelada de uma run, ainda fora da transação."""

    source_state: SourceState
    comparison: ComparisonMode | None = None
    flow_key: str | None = None
    branch: str | None = None
    head: str | None = None
    base_ref: str | None = None
    base_commit: str | None = None
    merge_base: str | None = None
    pr: JsonObject | None = None
    baseline_run_id: str | None = None
    dirty: bool = False
    remote_freshness: RemoteFreshness = RemoteFreshness.NOT_CHECKED
    remote_updated_at: str | None = None
    origin_identity: str | None = None
    files: tuple[ChangedFile, ...] = ()
    exclusions: tuple[Exclusion, ...] = ()
    truncated: bool = False
    unstable: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    limits: JsonObject = field(default_factory=dict[str, JsonValue])
    counts: JsonObject = field(default_factory=dict[str, JsonValue])
    scanner: JsonObject | None = None
    diff_text: str = ""
    diff_truncated: bool = False
    summary: str = ""
    sources: tuple[SourceFile, ...] = ()
    security: tuple[SecurityNotice, ...] = ()

    @property
    def analyzed_code_version(self) -> str | None:
        if self.pr is not None:
            head = self.pr.get("head_oid")
            return head if isinstance(head, str) else None
        return self.head

    def payload(self) -> JsonObject:
        """Manifest sanitizado: refs, camadas, limites, scanner, exclusões e avisos."""
        return {
            "schema_version": CHANGE_CONTEXT_SCHEMA_VERSION,
            "source_state": self.source_state.value,
            "comparison": self.comparison.value if self.comparison else None,
            "flow_key": self.flow_key,
            "branch": self.branch,
            "head": self.head,
            "base_ref": self.base_ref,
            "base_commit": self.base_commit,
            "merge_base": self.merge_base,
            "pr": self.pr,
            "baseline_run_id": self.baseline_run_id,
            "dirty": self.dirty,
            "remote_freshness": self.remote_freshness.value,
            "remote_updated_at": self.remote_updated_at,
            "origin_identity": self.origin_identity,
            "analyzed_code_version": self.analyzed_code_version,
            "files": [item.as_json() for item in self.files],
            "exclusions": [item.as_json() for item in self.exclusions],
            "excluded_count": len(self.exclusions),
            "truncated": self.truncated,
            "diff_truncated": self.diff_truncated,
            "unstable": list[JsonValue](self.unstable),
            "warnings": list[JsonValue](self.warnings),
            "limits": self.limits,
            "counts": self.counts,
            "scanner": self.scanner,
            "sources": [item.as_json() for item in self.sources],
            "summary_sha256": sha256_text(self.summary),
            "diff_sha256": sha256_text(self.diff_text),
        }


@dataclass(frozen=True)
class PreparedContext:
    """Tudo que a criação atômica da run grava junto: snapshot, captura e eventos de segurança."""

    issue: IssueCapture | None = None
    change: ChangeContextDraft | None = None

    @property
    def security(self) -> tuple[SecurityNotice, ...]:
        return self.change.security if self.change is not None else ()
