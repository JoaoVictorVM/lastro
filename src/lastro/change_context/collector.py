"""Coleta do contexto de mudança no processo confiável, sem chamar modelo nem o sistema alvo.

Ordem fixa: enumerar nomes, modos e tamanhos; aplicar negações e allowlist; só então ler os
buffers aprovados; varrer todos com o scanner antes de produzir qualquer diff; excluir por
inteiro o arquivo com possível segredo (inclusive em linhas removidas); truncar por último.
"""

import dataclasses
import difflib
import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from lastro.change_context.git import (
    Entry,
    Git,
    GitError,
    GitUnavailable,
    git_blob_oid,
    normalize_remote,
)
from lastro.change_context.issues import GhRunner, gh_get, parse_pr_url, run_gh
from lastro.change_context.models import (
    EXCLUDED_SECRET,
    ChangeContextDraft,
    ChangedFile,
    ComparisonMode,
    Exclusion,
    FileOrigin,
    JsonObject,
    Layer,
    RemoteFreshness,
    SecurityNotice,
    SourceFile,
    SourceState,
)
from lastro.change_context.scanner import Gitleaks
from lastro.change_context.source import (
    BINARY,
    NOT_ALLOWED,
    SECURITY_REASONS,
    UNSTABLE,
    SafeReader,
    SourcePolicy,
    UnsafeFile,
    is_text,
)
from lastro.config.source import SourceSettings
from lastro.history.database import db_timestamp
from lastro.history.redaction import REDACTED, redact_text
from lastro.timeutil import utc_now

NOT_GIT_REPOSITORY = "change_context_skipped: not_git_repository"
SECRET = "secret"
LIMIT = "limit"
# Caminho que a redação altera nunca entra no corpus: o nome já carrega um possível segredo.
SENSITIVE_PATH = "sensitive_path"
TRUNCATED = "\n[truncated]"

type Baseline = Callable[[str], tuple[str, str] | None]


class SourcePreflightError(Exception):
    """Preflight de --source falhou antes da run; a mensagem já é segura."""


@dataclass(frozen=True)
class CollectRequest:
    mode: ComparisonMode = ComparisonMode.WORKING_TREE
    base: str | None = None
    pr_url: str | None = None
    flow_key: str | None = None
    fetch: bool = False


@dataclass
class _Version:
    """Um buffer candidato de um caminho numa camada (base, commit, índice ou working tree)."""

    origin: FileOrigin
    oid: str | None
    size: int | None
    data: bytes | None = None
    text: str | None = None


@dataclass
class _PathState:
    path: str
    versions: dict[str, _Version] = field(default_factory=dict[str, _Version])
    excluded: str | None = None


def _safe_path(path: str) -> str:
    return redact_text(path)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _label(reason: str | None) -> str | None:
    if reason == SECRET:
        return EXCLUDED_SECRET
    if reason == NOT_ALLOWED:
        return "excluded (not allowed)"
    return reason


def _status(old: Entry | None, new: Entry | None) -> str:
    if old is None:
        return "added"
    if new is None:
        return "deleted"
    return "modified"


def _same_blob(oid: str, data: bytes) -> bool:
    """Compara sem `git status`, tolerando conversão CRLF do checkout."""
    length = len(oid)
    return oid in (git_blob_oid(data, length), git_blob_oid(data.replace(b"\r\n", b"\n"), length))


class Collector:
    def __init__(
        self,
        settings: SourceSettings,
        root: Path,
        *,
        scanner: Gitleaks,
        gh_runner: GhRunner | None = None,
        baseline: Baseline | None = None,
        git: Git | None = None,
        reader: SafeReader | None = None,
    ) -> None:
        self.settings = settings
        self.policy = SourcePolicy(tuple(settings.allow_paths))
        self.root = root
        self.scanner = scanner
        self.gh_runner = gh_runner or run_gh
        self.baseline = baseline
        self._git = git
        self.reader = reader or SafeReader(root)
        self.nonce = secrets.token_hex(8)
        self.warnings: list[str] = []
        self.security: list[SecurityNotice] = []
        self.exclusions: dict[str, str] = {}
        self.index: dict[str, Entry] = {}
        self.dirty = False
        self.truncated = False
        self.total_bytes = 0

    @property
    def git(self) -> Git:
        if self._git is None:
            self._git = Git(self.root)
        return self._git

    def _skipped(self, request: CollectRequest) -> ChangeContextDraft:
        return ChangeContextDraft(
            source_state=SourceState.SKIPPED,
            comparison=request.mode,
            flow_key=request.flow_key,
            warnings=(NOT_GIT_REPOSITORY,),
        )

    def collect(self, request: CollectRequest) -> ChangeContextDraft:
        try:
            if not self.git.is_top_level():
                return self._skipped(request)
        except GitUnavailable:
            return self._skipped(request)
        try:
            return self._collect(request)
        finally:
            self.git.close()

    # Comparação ---------------------------------------------------------------------------

    def _origin_identity(self) -> str | None:
        url = self.git.remotes().get("origin")
        return normalize_remote(url) if url is not None else None

    def _fetch(self, request: CollectRequest, base_ref: str | None) -> RemoteFreshness:
        if not request.fetch:
            return RemoteFreshness.NOT_CHECKED
        remote = "origin"
        if base_ref is not None and "/" in base_ref:
            candidate = base_ref.split("/", 1)[0]
            if candidate in self.git.remotes():
                remote = candidate
        try:
            self.git.fetch(remote)
        except GitError:
            self.warnings.append("fetch_failed: remote data may be stale")
            return RemoteFreshness.FETCH_FAILED
        return RemoteFreshness.FETCHED

    def _pr(self, request: CollectRequest) -> tuple[JsonObject, str | None, str | None]:
        """Metadados do PR por GET; objetos ausentes tornam a comparação indisponível."""
        if request.pr_url is None:
            raise SourcePreflightError("--compare pr requires --pr URL.")
        ref = parse_pr_url(request.pr_url)
        data = gh_get(self.gh_runner, f"repos/{ref.repository}/pulls/{ref.number}", "pull request")
        if not isinstance(data, dict):
            raise SourcePreflightError("Could not read pull request: unexpected response.")

        def text_at(*keys: str) -> str | None:
            value = cast(object, data)
            for key in keys:
                if not isinstance(value, dict):
                    return None
                value = cast(dict[str, object], value).get(key)
            return value if isinstance(value, str) else None

        base_oid, head_oid = text_at("base", "sha"), text_at("head", "sha")
        base_repo = text_at("base", "repo", "full_name")
        head_repo = text_at("head", "repo", "full_name")
        info: JsonObject = {
            "url": f"https://github.com/{ref.repository}/pull/{ref.number}",
            "number": ref.number,
            "base_repository": base_repo,
            "head_repository": head_repo,
            "fork": head_repo is not None and head_repo != base_repo,
            "base_oid": base_oid,
            "head_oid": head_oid,
        }
        origin = self._origin_identity()
        if base_repo is None or origin is None or not origin.endswith("/" + base_repo):
            self.warnings.append("comparison unavailable: pull request is not for this repository")
            return info, None, None
        if not (base_oid and head_oid):
            self.warnings.append("comparison unavailable: pull request metadata incomplete")
            return info, None, None
        if not (self.git.has_commit(base_oid) and self.git.has_commit(head_oid)):
            # PR de fork sem objetos verificáveis nunca é analisado como se fosse o HEAD local.
            self.warnings.append(
                "comparison unavailable: pull request objects missing locally (use --fetch)"
            )
            return info, None, None
        return info, base_oid, head_oid

    def _range(
        self, request: CollectRequest, head: str, base_ref: str | None
    ) -> tuple[str | None, str, str | None, str | None, JsonObject | None, str | None]:
        """(antigo, novo, base, merge base, PR, run de baseline); nunca uma base substituta."""
        git = self.git
        if request.mode in (ComparisonMode.FEATURE, ComparisonMode.WORKING_TREE):
            if base_ref is None:
                self.warnings.append(
                    "comparison unavailable: configure [source] base_ref or --base"
                )
                return None, head, None, None, None, None
            base_commit = git.resolve(base_ref)
            merge_base = git.merge_base(base_commit, head) if base_commit else None
            if base_commit is None or merge_base is None:
                self.warnings.append("comparison unavailable: base or merge base not found")
            return merge_base, head, base_commit, merge_base, None, None
        if request.mode == ComparisonMode.PR:
            info, base_oid, head_oid = self._pr(request)
            if base_oid is None or head_oid is None:
                return None, "", None, None, info, None
            merge_base = git.merge_base(base_oid, head_oid)
            if merge_base is None:
                self.warnings.append("comparison unavailable: pull request merge base not found")
            return merge_base, head_oid, base_oid, merge_base, info, None
        found = self.baseline(request.flow_key) if self.baseline and request.flow_key else None
        if found is None:
            self.warnings.append("comparison unavailable: no eligible last-passed baseline")
            return None, head, None, None, None, None
        if not git.has_commit(found[1]):
            self.warnings.append("comparison unavailable: baseline commit missing locally")
            return None, head, None, None, None, found[0]
        return found[1], head, found[1], None, None, found[0]

    # Coleta -------------------------------------------------------------------------------

    def _collect(self, request: CollectRequest) -> ChangeContextDraft:
        git = self.git
        mode = request.mode
        local_modes = (ComparisonMode.FEATURE, ComparisonMode.WORKING_TREE)
        base_ref = request.base or (self.settings.base_ref if mode in local_modes else None)
        freshness = self._fetch(request, base_ref)
        fetched_at = db_timestamp(utc_now()) if freshness == RemoteFreshness.FETCHED else None
        head = git.resolve("HEAD")
        if head is None:
            self.warnings.append("comparison unavailable: HEAD has no commit")
            return ChangeContextDraft(
                source_state=SourceState.UNAVAILABLE,
                comparison=mode,
                flow_key=request.flow_key,
                warnings=tuple(self.warnings),
            )
        branch = git.branch()
        index_before = git.index_identity()
        old, new, base_commit, merge_base, pr_info, baseline_run = self._range(
            request, head, base_ref
        )
        states: dict[str, _PathState] = {}
        files: list[ChangedFile] = []
        if new:
            new_tree = git.ls_tree(new)
            old_tree = git.ls_tree(old) if old else {}
            files = self._enumerate(mode, states, old_tree, new_tree, compare=old is not None)
        self.truncated = self._limit(states)
        self._read(states)
        files += self._detect_unstaged(states)
        self._read(states)
        self._scan(states)
        if git.resolve("HEAD") != head or git.index_identity() != index_before:
            # Uma coleta inconsistente nunca é entregue; não há retry automático.
            self.warnings.append("unstable: HEAD or index changed during capture")
            for state in states.values():
                state.excluded = state.excluded or UNSTABLE
                state.versions.clear()
        sources = self._sources(states, mode)
        ids = {item.relative_path: item.file_id for item in sources}
        diff_text, diff_truncated = self._diff(states, files)
        changed = tuple(
            ChangedFile(
                _safe_path(item.path),
                item.layer,
                item.status,
                ids.get(item.path) if item.status != "deleted" else None,
                _label(self.exclusions.get(item.path) or states[item.path].excluded)
                if item.path in states
                else None,
            )
            for item in files
        )
        for path, state in states.items():
            if state.excluded and state.excluded != NOT_ALLOWED:
                self.exclusions.setdefault(path, state.excluded)
        exclusions = tuple(
            Exclusion(_safe_path(path), _label(reason) or reason)
            for path, reason in sorted(self.exclusions.items())
        )
        comparison_ok = not any(item.startswith("comparison unavailable") for item in self.warnings)
        partial = self.truncated or diff_truncated or not comparison_ok or bool(exclusions)
        draft = ChangeContextDraft(
            source_state=SourceState.PARTIAL if partial else SourceState.AVAILABLE,
            comparison=mode,
            flow_key=request.flow_key,
            branch=_safe_path(branch) if branch else None,
            head=head,
            base_ref=base_ref,
            base_commit=base_commit,
            merge_base=merge_base,
            pr=pr_info,
            baseline_run_id=baseline_run,
            dirty=self.dirty if mode == ComparisonMode.WORKING_TREE else False,
            remote_freshness=freshness,
            remote_updated_at=fetched_at,
            origin_identity=self._origin_identity(),
            files=changed,
            exclusions=exclusions,
            truncated=self.truncated,
            unstable=tuple(sorted(p for p, s in states.items() if s.excluded == UNSTABLE)),
            warnings=tuple(self.warnings),
            limits={
                "max_candidates": self.settings.max_candidates,
                "max_total_bytes": self.settings.max_total_bytes,
                "max_diff_characters": self.settings.max_diff_characters,
                "max_summary_characters": self.settings.max_summary_characters,
            },
            counts={
                "candidates": sum(1 for s in states.values() if s.excluded != NOT_ALLOWED),
                "sources": len(sources),
                "changed": len(changed),
                "excluded": len(exclusions),
                "not_allowed": sum(1 for s in states.values() if s.excluded == NOT_ALLOWED),
                "authorized_bytes": self.total_bytes,
            },
            scanner=dict(self.scanner.metadata()),  # pyright: ignore[reportArgumentType]
            diff_text=diff_text,
            diff_truncated=diff_truncated,
            sources=sources,
            security=tuple(self.security),
        )
        return dataclasses.replace(draft, summary=self._summary(draft))

    def _deny(self, path: str, reason: str) -> None:
        if reason in SECURITY_REASONS:
            self.security.append(SecurityNotice(reason, _safe_path(path)))
        if reason != NOT_ALLOWED:
            self.exclusions.setdefault(path, reason)

    def _state(self, states: dict[str, _PathState], path: str) -> _PathState:
        return states.setdefault(path, _PathState(path))

    def _exclude(self, state: _PathState, reason: str) -> None:
        state.excluded = state.excluded or reason
        state.versions.clear()
        self._deny(state.path, reason)

    def _admit(
        self, states: dict[str, _PathState], entry: Entry, key: str, origin: FileOrigin
    ) -> None:
        """Política por caminho, nome, modo e tamanho antes de registrar qualquer leitura."""
        state = self._state(states, entry.path)
        if state.excluded:
            return
        reason = self.policy.classify_entry(entry.path, entry.mode, entry.size)
        if reason is None and _safe_path(entry.path) != entry.path:
            reason = SENSITIVE_PATH
        if reason is not None:
            self._exclude(state, reason)
            return
        state.versions[key] = _Version(origin, entry.oid, entry.size)

    def _enumerate(
        self,
        mode: ComparisonMode,
        states: dict[str, _PathState],
        old_tree: dict[str, Entry],
        new_tree: dict[str, Entry],
        *,
        compare: bool,
    ) -> list[ChangedFile]:
        files: list[ChangedFile] = []
        local = mode == ComparisonMode.WORKING_TREE
        for path in sorted(set(old_tree) | set(new_tree)):
            old, new = old_tree.get(path), new_tree.get(path)
            changed = compare and not (old is not None and new is not None and old.oid == new.oid)
            if new is not None and (changed or not local):
                self._admit(states, new, "commit", FileOrigin.COMMIT)
            if not changed:
                continue
            if old is not None:
                self._admit(states, old, "base", FileOrigin.BASE)
            files.append(ChangedFile(path, Layer.COMMITTED, _status(old, new)))
        if local:
            files += self._enumerate_local(states, new_tree)
        return files

    def _enumerate_local(
        self, states: dict[str, _PathState], head_tree: dict[str, Entry]
    ) -> list[ChangedFile]:
        files: list[ChangedFile] = []
        index, conflicted = self.git.ls_index()
        self.index = index
        for path in sorted(conflicted):
            self._exclude(self._state(states, path), UNSTABLE)
            self.dirty = True
        for path in sorted(set(index) | set(head_tree)):
            head_entry, staged = head_tree.get(path), index.get(path)
            if path in conflicted or (
                head_entry is not None and staged is not None and head_entry.oid == staged.oid
            ):
                continue
            if head_entry is not None:
                self._admit(states, head_entry, "commit", FileOrigin.COMMIT)
            if staged is not None:
                self._admit(states, staged, "index", FileOrigin.INDEX)
            files.append(ChangedFile(path, Layer.STAGED, _status(head_entry, staged)))
            self.dirty = True
        for path in sorted(index):
            state = self._state(states, path)
            if state.excluded:
                continue
            reason = self.policy.classify(path)
            if reason is None and _safe_path(path) != path:
                reason = SENSITIVE_PATH
            if reason is not None:
                self._exclude(state, reason)
                continue
            try:
                info = self.reader.lstat(path)
            except FileNotFoundError:
                files.append(ChangedFile(path, Layer.UNSTAGED, "deleted"))
                self.dirty = True
                continue
            except UnsafeFile as error:
                self._exclude(state, error.reason)
                continue
            reason = self.policy.classify_entry(path, index[path].mode, info.st_size)
            if reason is not None:
                self._exclude(state, reason)
                continue
            state.versions["worktree"] = _Version(FileOrigin.WORKTREE, None, info.st_size)
        for path in self.git.untracked():
            state = self._state(states, path)
            # Negação por nome antes de qualquer lstat ou abertura do arquivo não rastreado.
            reason = self.policy.classify(path)
            if reason is None and _safe_path(path) != path:
                reason = SENSITIVE_PATH
            size = 0
            if reason is None:
                try:
                    size = self.reader.lstat(path).st_size
                    reason = self.policy.classify_entry(path, "100644", size)
                except FileNotFoundError:
                    reason = UNSTABLE
                except UnsafeFile as error:
                    reason = error.reason
            if reason is not None:
                self._exclude(state, reason)
                continue
            state.versions["worktree"] = _Version(FileOrigin.WORKTREE, None, size)
            files.append(ChangedFile(path, Layer.UNTRACKED, "added"))
            self.dirty = True
        return files

    def _limit(self, states: dict[str, _PathState]) -> bool:
        admitted = [path for path in sorted(states) if not states[path].excluded]
        overflow = admitted[self.settings.max_candidates :]
        for path in overflow:
            states[path].excluded = LIMIT
            states[path].versions.clear()
        return bool(overflow)

    def _read(self, states: dict[str, _PathState]) -> None:
        for path in sorted(states):
            state = states[path]
            if state.excluded:
                continue
            for key in ("base", "commit", "index", "worktree"):
                version = state.versions.get(key)
                if version is None or version.text is not None:
                    continue
                try:
                    if version.origin == FileOrigin.WORKTREE:
                        data = self.reader.read(path)
                    else:
                        assert version.oid is not None and version.size is not None
                        data = self.git.blob(version.oid, version.size)
                except UnsafeFile as error:
                    self._exclude(state, error.reason)
                    break
                except GitError:
                    self._exclude(state, UNSTABLE)
                    break
                if not is_text(data):
                    self._exclude(state, BINARY)
                    break
                self.total_bytes += len(data)
                if self.total_bytes > self.settings.max_total_bytes:
                    state.excluded = LIMIT
                    state.versions.clear()
                    self.truncated = True
                    break
                version.data = data
                version.text = data.decode("utf-8")

    def _detect_unstaged(self, states: dict[str, _PathState]) -> list[ChangedFile]:
        """Working tree contra índice por OID calculado, sem `git status` nem filtros."""
        files: list[ChangedFile] = []
        for path, state in sorted(states.items()):
            worktree = state.versions.get("worktree")
            staged = self.index.get(path)
            if state.excluded or worktree is None or worktree.data is None or staged is None:
                continue
            if _same_blob(staged.oid, worktree.data):
                continue
            state.versions.setdefault("index", _Version(FileOrigin.INDEX, staged.oid, staged.size))
            files.append(ChangedFile(path, Layer.UNSTAGED, "modified"))
            self.dirty = True
        return files

    def _scan(self, states: dict[str, _PathState]) -> None:
        """Todo buffer autorizado e completo, inclusive base e índice, antes de qualquer diff."""
        buffers: list[tuple[str, str]] = [
            (path, version.text)
            for path, state in sorted(states.items())
            if not state.excluded
            for version in state.versions.values()
            if version.text is not None
        ]
        found = self.scanner.findings([text for _, text in buffers])
        for (path, _), lines in zip(buffers, found, strict=True):
            if lines:
                states[path].excluded = SECRET
                states[path].versions.clear()

    def _file_id(self, path: str, origin: FileOrigin) -> str:
        return "file_" + _sha(f"{self.nonce}:{origin.value}:{path}")[:16]

    def _sources(
        self, states: dict[str, _PathState], mode: ComparisonMode
    ) -> tuple[SourceFile, ...]:
        """Corpus permitido da revisão analisada: a versão mais recente de cada caminho."""
        key = "worktree" if mode == ComparisonMode.WORKING_TREE else "commit"
        sources: list[SourceFile] = []
        for path, state in sorted(states.items()):
            version = state.versions.get(key)
            if state.excluded or version is None or version.text is None:
                continue
            sources.append(
                SourceFile(
                    file_id=self._file_id(path, version.origin),
                    relative_path=path,
                    origin=version.origin,
                    revision_oid=version.oid,
                    content=version.text,
                )
            )
        return tuple(sources)

    def _layer_pair(self, state: _PathState, layer: Layer) -> tuple[str | None, str | None]:
        def text(key: str) -> str | None:
            version = state.versions.get(key)
            return version.text if version is not None else None

        if layer == Layer.COMMITTED:
            return text("base"), text("commit")
        if layer == Layer.STAGED:
            return text("commit"), text("index")
        if layer == Layer.UNSTAGED:
            return text("index"), text("worktree")
        return None, text("worktree")

    def _diff(self, states: dict[str, _PathState], files: list[ChangedFile]) -> tuple[str, bool]:
        """Diferenças calculadas no Python entre buffers aprovados, nunca um diff global."""
        sections: list[tuple[str, str, str]] = []
        for item in files:
            state = states.get(item.path)
            header = f"--- {item.layer.value}: {_safe_path(item.path)} ({item.status})"
            if state is None or state.excluded:
                reason = state.excluded if state is not None else "unavailable"
                sections.append((item.path, header, f"{header}\n{REDACTED} {_label(reason)}\n"))
                continue
            before, after = self._layer_pair(state, item.layer)
            body = "".join(
                difflib.unified_diff(
                    (before or "").splitlines(keepends=True),
                    (after or "").splitlines(keepends=True),
                    fromfile="a/" + _safe_path(item.path) if before is not None else "/dev/null",
                    tofile="b/" + _safe_path(item.path) if after is not None else "/dev/null",
                    n=3,
                )
            )
            sections.append((item.path, header, f"{header}\n{body}"))
        # Reexame do diff sanitizado: contexto, adições e remoções juntos.
        found = self.scanner.findings([text for _, _, text in sections]) if sections else []
        rendered: list[str] = []
        for (path, header, text), lines in zip(sections, found, strict=True):
            if lines:
                self.exclusions[path] = SECRET
                rendered.append(f"{header}\n{REDACTED} {EXCLUDED_SECRET}\n")
            else:
                rendered.append(text)
        diff = "\n".join(rendered)
        limit = self.settings.max_diff_characters
        if len(diff) > limit:
            return diff[: limit - len(TRUNCATED)] + TRUNCATED, True
        return diff, False

    def _summary(self, draft: ChangeContextDraft) -> str:
        lines = [
            f"Comparison: {draft.comparison.value if draft.comparison else 'none'}",
            f"Branch: {draft.branch or '(detached)'}",
            f"HEAD: {draft.head}",
            f"Base: {draft.base_ref or '-'} ({draft.base_commit or 'unavailable'})",
            f"Merge base: {draft.merge_base or 'unavailable'}",
            f"Dirty: {'yes' if draft.dirty else 'no'}",
            f"Remote freshness: {draft.remote_freshness.value}",
            "",
            "Changed files (implementation hypotheses, not business criteria):",
        ]
        lines += [
            f"- [{item.file_id or '-'}] {item.layer.value} {item.status} {item.path}"
            + (f" ({item.excluded_reason})" if item.excluded_reason else "")
            for item in draft.files
        ] or ["- none"]
        if draft.exclusions:
            lines += ["", "Excluded:"]
            lines += [f"- {item.path}: {item.reason}" for item in draft.exclusions]
        if draft.warnings:
            lines += ["", "Warnings:", *(f"- {item}" for item in draft.warnings)]
        if draft.truncated or draft.diff_truncated:
            lines += ["", "Result is partial: limits reached; coverage is not complete."]
        header = "\n".join(lines) + "\n\nDiff:\n"
        limit = self.settings.max_summary_characters
        if len(header) >= limit:
            return header[: limit - len(TRUNCATED)] + TRUNCATED
        budget = limit - len(header)
        diff = draft.diff_text
        if len(diff) > budget:
            diff = diff[: max(0, budget - len(TRUNCATED))] + TRUNCATED
        return header + diff
