"""Importação de issues: GitHub por gh API (somente GET), exportação Markdown e texto local.

Nada aqui segue URLs do corpo, baixa anexos, pagina sem limite ou faz login. Erros nunca
repassam stdout/stderr bruto do gh.
"""

import json
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlsplit

from pydantic import ValidationError

from lastro.change_context.models import (
    MAX_CAPTURE_CHARACTERS,
    MAX_COMMENT_BODY,
    MAX_COMMENTS,
    MAX_ISSUE_BODY,
    MAX_ISSUE_JSON_BYTES,
    IssueCapture,
    IssueComment,
    IssueContent,
    ScanLevel,
    SourceKind,
)
from lastro.history.database import db_timestamp
from lastro.history.redaction import redact_text
from lastro.timeutil import utc_now

GH_TIMEOUT_SECONDS = 30.0
MAX_ISSUE_FILE_BYTES = 1_000_000
DEFAULT_TITLE = "Imported issue"
_OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"
_REPO = r"[A-Za-z0-9._-]{1,100}"
_ISSUE_PATH = re.compile(
    rf"^/(?P<owner>{_OWNER})/(?P<repo>{_REPO})/issues/(?P<number>[1-9]\d{{0,9}})$"
)
_PR_PATH = re.compile(rf"^/(?P<owner>{_OWNER})/(?P<repo>{_REPO})/pull/(?P<number>[1-9]\d{{0,9}})$")
_HTTP_STATUS = re.compile(r"\bHTTP (\d{3})\b")
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
_FRONT_KEYS = frozenset({"repository", "number", "url", "title", "labels"})

AUTH_HINT = "Check 'gh auth status' and repository access."


class IssueImportError(Exception):
    """Falha de importação antes de qualquer run; a mensagem já é segura para o terminal."""


@dataclass(frozen=True)
class GhResult:
    returncode: int
    stdout: bytes
    stderr: bytes


type GhRunner = Callable[[Sequence[str], float], GhResult]
type Clock = Callable[[], datetime]


class Sanitizer(Protocol):
    """Redige textos importados; informa se o scanner de segredos participou."""

    def __call__(self, texts: list[str]) -> tuple[list[str], ScanLevel]: ...


def redaction_only(texts: list[str]) -> tuple[list[str], ScanLevel]:
    return [redact_text(text) for text in texts], ScanLevel.REDACTION_ONLY


@dataclass(frozen=True)
class GitHubRef:
    owner: str
    repo: str
    number: int

    @property
    def repository(self) -> str:
        return f"{self.owner}/{self.repo}"


def _parse_github(url: str, pattern: re.Pattern[str], kind: str) -> GitHubRef:
    if any(char in url for char in "?#@\\") or any(char.isspace() for char in url):
        raise IssueImportError(f"Invalid {kind} URL: credentials, query and fragment are refused.")
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != "github.com":
        raise IssueImportError(
            f"Invalid {kind} URL: only https://github.com/<owner>/<repo>/... is supported."
        )
    match = pattern.match(parts.path)
    if match is None:
        raise IssueImportError(f"Invalid {kind} URL: expected {pattern_hint(kind)}.")
    return GitHubRef(match["owner"], match["repo"], int(match["number"]))


def pattern_hint(kind: str) -> str:
    tail = "issues/<n>" if kind == "issue" else "pull/<n>"
    return f"https://github.com/<owner>/<repo>/{tail}"


def parse_issue_url(url: str) -> GitHubRef:
    return _parse_github(url, _ISSUE_PATH, "issue")


def parse_pr_url(url: str) -> GitHubRef:
    return _parse_github(url, _PR_PATH, "pull request")


def _gh_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GH_PROMPT_DISABLED": "1",
            "GH_NO_UPDATE_NOTIFIER": "1",
            "GH_PAGER": "cat",
            "NO_COLOR": "1",
            "GH_SPINNER_DISABLED": "1",
        }
    )
    return env


def run_gh(args: Sequence[str], timeout: float) -> GhResult:
    executable = shutil.which("gh")
    if executable is None:
        raise IssueImportError(
            "GitHub CLI (gh) not found. Install it and run 'gh auth login'; "
            "or use --issue-file/--issue-text."
        )
    try:
        completed = subprocess.run(
            [executable, *args],
            capture_output=True,
            timeout=timeout,
            env=_gh_env(),
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise IssueImportError(
            f"GitHub CLI timed out after {int(timeout)} s; check connectivity and retry."
        ) from None
    except OSError:
        raise IssueImportError("GitHub CLI (gh) could not be started.") from None
    return GhResult(completed.returncode, completed.stdout, completed.stderr)


def _diagnose(result: GhResult, subject: str) -> IssueImportError:
    stderr = result.stderr.decode("utf-8", "replace")
    lowered = stderr.lower()
    status = _HTTP_STATUS.search(stderr)
    if "rate limit" in lowered:
        return IssueImportError(
            f"Could not read {subject}: GitHub API rate limit exceeded. Retry later; "
            "no automatic retry was made."
        )
    if status is not None and status.group(1) in ("403", "404"):
        return IssueImportError(f"Could not read {subject} (HTTP {status.group(1)}). {AUTH_HINT}")
    if (status is not None and status.group(1) == "401") or "auth login" in lowered:
        return IssueImportError(
            f"Could not read {subject}: GitHub CLI is not authenticated. Run 'gh auth login'."
        )
    if status is not None:
        return IssueImportError(f"Could not read {subject} (HTTP {status.group(1)}). {AUTH_HINT}")
    return IssueImportError(
        f"Could not read {subject} (gh exited with code {result.returncode}). {AUTH_HINT}"
    )


def gh_get(runner: GhRunner, path: str, subject: str) -> object:
    """Uma consulta GET explícita, sem paginação automática e com teto de 1 MB."""
    result = runner(
        [
            "api",
            "--hostname",
            "github.com",
            "--method",
            "GET",
            "-H",
            "Accept: application/vnd.github+json",
            path,
        ],
        GH_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise _diagnose(result, subject)
    if len(result.stdout) > MAX_ISSUE_JSON_BYTES:
        raise IssueImportError(
            f"Could not read {subject}: response exceeds {MAX_ISSUE_JSON_BYTES} bytes; "
            "the run was not started."
        )
    try:
        return json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise IssueImportError(f"Could not read {subject}: unexpected response.") from None


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _optional(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _check_limits(body: str, comments: list[str]) -> None:
    if len(body) > MAX_ISSUE_BODY:
        raise IssueImportError(
            f"Issue body exceeds {MAX_ISSUE_BODY} characters; the run was not started so "
            "no criteria are silently dropped."
        )
    if any(len(item) > MAX_COMMENT_BODY for item in comments):
        raise IssueImportError(
            f"An issue comment exceeds {MAX_COMMENT_BODY} characters; the run was not started."
        )
    if len(body) + sum(len(item) for item in comments) > MAX_CAPTURE_CHARACTERS:
        raise IssueImportError(
            f"Issue capture exceeds {MAX_CAPTURE_CHARACTERS} characters; the run was not started."
        )


def _build(
    *,
    kind: SourceKind,
    title: str,
    body: str,
    labels: list[str],
    comments: list[dict[str, object]],
    comments_total: int,
    repository: str | None,
    number: int | None,
    url: str | None,
    updated_at: str | None,
    sanitizer: Sanitizer,
) -> tuple[IssueContent, ScanLevel]:
    raw_comments = [_text(item.get("body")) for item in comments]
    _check_limits(body, raw_comments)
    sanitized, level = sanitizer([title, body, *labels, *raw_comments])
    title_s, body_s = sanitized[0], sanitized[1]
    labels_s = sanitized[2 : 2 + len(labels)]
    comments_s = sanitized[2 + len(labels) :]
    try:
        content = IssueContent(
            source_kind=kind,
            repository=repository,
            number=number,
            url=url,
            title=title_s.strip() or DEFAULT_TITLE,
            body=body_s,
            labels=tuple(sorted(set(labels_s))),
            comments=tuple(
                IssueComment(
                    source_id=_optional(str(item["id"]) if "id" in item else None),
                    body=text,
                    created_at=_optional(item.get("created_at")),
                    updated_at=_optional(item.get("updated_at")),
                )
                for item, text in zip(comments, comments_s, strict=True)
            ),
            comments_total=comments_total,
            comments_included=len(comments),
            comments_truncated=comments_total > len(comments),
            source_updated_at=updated_at,
        )
    except ValidationError:
        raise IssueImportError("Imported issue exceeds the supported limits.") from None
    return content, level


def _comment_key(item: dict[str, object]) -> tuple[str, int]:
    identifier = item.get("id")
    return (_text(item.get("created_at")), identifier if isinstance(identifier, int) else 0)


def fetch_github_issue(
    url: str,
    *,
    runner: GhRunner | None = None,
    sanitizer: Sanitizer = redaction_only,
    clock: Clock = utc_now,
) -> IssueCapture:
    """Título, corpo, labels e os primeiros 50 comentários por ordem de criação.

    O GitHub não oferece snapshot transacional: registramos início, fim e o `updated_at`.
    """
    runner = runner or run_gh
    ref = parse_issue_url(url)
    started = db_timestamp(clock())
    data = gh_get(runner, f"repos/{ref.repository}/issues/{ref.number}", "issue")
    if not isinstance(data, dict):
        raise IssueImportError("Could not read issue: unexpected response.")
    issue = cast(dict[str, object], data)
    if "pull_request" in issue:
        raise IssueImportError(
            "The URL points to a pull request; use --pr with --compare pr for comparisons."
        )
    html_url = _text(issue.get("html_url"))
    if html_url and urlsplit(html_url).netloc != "github.com":
        raise IssueImportError("Could not read issue: response redirected to a different host.")
    total_value = issue.get("comments")
    total = total_value if isinstance(total_value, int) and total_value >= 0 else 0
    comments: list[dict[str, object]] = []
    if total > 0:
        listed = gh_get(
            runner,
            f"repos/{ref.repository}/issues/{ref.number}/comments?per_page={MAX_COMMENTS}&page=1",
            "issue comments",
        )
        if not isinstance(listed, list):
            raise IssueImportError("Could not read issue comments: unexpected response.")
        items = [
            cast(dict[str, object], item)
            for item in cast(list[object], listed)
            if isinstance(item, dict)
        ]
        comments = sorted(items, key=_comment_key)[:MAX_COMMENTS]
    labels_value = issue.get("labels")
    labels = [
        _text(cast(dict[str, object], item).get("name"))
        for item in (cast(list[object], labels_value) if isinstance(labels_value, list) else [])
        if isinstance(item, dict)
    ]
    content, level = _build(
        kind=SourceKind.GITHUB,
        title=_text(issue.get("title")),
        body=_text(issue.get("body")),
        labels=[label for label in labels if label],
        comments=comments,
        comments_total=max(total, len(comments)),
        repository=ref.repository,
        number=ref.number,
        url=f"https://github.com/{ref.repository}/issues/{ref.number}",
        updated_at=_optional(issue.get("updated_at")),
        sanitizer=sanitizer,
    )
    return IssueCapture(content, started, db_timestamp(clock()), level)


def _front_matter(text: str) -> tuple[dict[str, str], str]:
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            metadata: dict[str, str] = {}
            for line in lines[1:index]:
                key, separator, value = line.partition(":")
                key = key.strip().lower()
                if separator and key in _FRONT_KEYS:
                    metadata[key] = value.strip().strip("'\"")
            return metadata, "\n".join(lines[index + 1 :]).lstrip("\n")
    return {}, text


def _first_heading(text: str) -> str | None:
    for line in text.split("\n"):
        match = _HEADING.match(line.strip())
        if match is not None:
            return match.group(1)
    return None


def _declared_identity(metadata: dict[str, str]) -> tuple[str | None, int | None, str | None]:
    url = metadata.get("url") or None
    repository = metadata.get("repository") or None
    number_text = metadata.get("number") or None
    number = int(number_text) if number_text and number_text.isdigit() else None
    if number_text and number is None:
        raise IssueImportError("Issue export metadata: 'number' must be a positive integer.")
    if url is not None:
        ref = parse_issue_url(url)
        if repository not in (None, ref.repository) or number not in (None, ref.number):
            raise IssueImportError("Issue export metadata: url, repository and number disagree.")
        return ref.repository, ref.number, url
    if repository is not None and not re.fullmatch(rf"{_OWNER}/{_REPO}", repository):
        raise IssueImportError("Issue export metadata: 'repository' must be owner/repo.")
    return repository, number, None


def read_issue_file(path: Path) -> str:
    """Exportação local: arquivo regular, sem link, UTF-8 e até 1 MB."""
    try:
        info = path.lstat()
    except OSError:
        raise IssueImportError(f"Issue file not found: {path.name}") from None
    attributes = getattr(info, "st_file_attributes", 0)
    if not stat.S_ISREG(info.st_mode) or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise IssueImportError("Issue file must be a regular file (links are refused).")
    if info.st_size > MAX_ISSUE_FILE_BYTES:
        raise IssueImportError(f"Issue file exceeds {MAX_ISSUE_FILE_BYTES} bytes.")
    with path.open("rb") as handle:
        if os.fstat(handle.fileno()).st_ino != info.st_ino and info.st_ino:
            raise IssueImportError("Issue file changed while being read.")
        data = handle.read(MAX_ISSUE_FILE_BYTES + 1)
    if len(data) > MAX_ISSUE_FILE_BYTES:
        raise IssueImportError(f"Issue file exceeds {MAX_ISSUE_FILE_BYTES} bytes.")
    try:
        return data.decode("utf-8-sig").replace("\r\n", "\n")
    except UnicodeDecodeError:
        raise IssueImportError("Issue file must be UTF-8.") from None


def import_markdown(
    text: str, *, sanitizer: Sanitizer = redaction_only, clock: Clock = utc_now
) -> IssueCapture:
    """Exportação Markdown; metadata declarada não vira coleta remota e nada é inferido."""
    started = db_timestamp(clock())
    metadata, body = _front_matter(text)
    if not body.strip() and not metadata.get("title"):
        raise IssueImportError("Issue export is empty.")
    repository, number, url = _declared_identity(metadata)
    labels = [item.strip() for item in metadata.get("labels", "").strip("[]").split(",")]
    content, level = _build(
        kind=SourceKind.MARKDOWN,
        title=metadata.get("title") or _first_heading(body) or DEFAULT_TITLE,
        body=body,
        labels=[label.strip("'\"") for label in labels if label],
        comments=[],
        comments_total=0,
        repository=repository,
        number=number,
        url=url,
        updated_at=None,
        sanitizer=sanitizer,
    )
    return IssueCapture(content, started, db_timestamp(clock()), level)


def import_text(
    text: str, *, sanitizer: Sanitizer = redaction_only, clock: Clock = utc_now
) -> IssueCapture:
    """Texto livre conserva o conteúdo como corpo, sem identidade GitHub."""
    started = db_timestamp(clock())
    if not text.strip():
        raise IssueImportError("--issue-text must not be empty.")
    normalized = text.replace("\r\n", "\n")
    content, level = _build(
        kind=SourceKind.TEXT,
        title=_first_heading(normalized) or DEFAULT_TITLE,
        body=normalized,
        labels=[],
        comments=[],
        comments_total=0,
        repository=None,
        number=None,
        url=None,
        updated_at=None,
        sanitizer=sanitizer,
    )
    return IssueCapture(content, started, db_timestamp(clock()), level)
