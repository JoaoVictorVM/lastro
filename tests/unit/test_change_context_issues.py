import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lastro.change_context import IssueCapture, SourceKind
from lastro.change_context.issues import (
    GhResult,
    IssueImportError,
    fetch_github_issue,
    import_markdown,
    import_text,
    parse_issue_url,
    read_issue_file,
)
from lastro.change_context.models import issue_requirements

URL = "https://github.com/org/repo/issues/123"


class FakeGh:
    def __init__(self, issue: dict[str, object], comments: list[dict[str, object]]) -> None:
        self.issue, self.comments = issue, comments
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], timeout: float) -> GhResult:
        self.calls.append(list(args))
        path = args[-1]
        data: object = self.comments if "/comments" in path else self.issue
        return GhResult(0, json.dumps(data).encode(), b"")


def _issue(**extra: object) -> dict[str, object]:
    return {
        "title": "Permitir email opcional",
        "body": "Critério: salvar cadastro sem email.",
        "labels": [{"name": "bug"}, {"name": "ux"}],
        "comments": 0,
        "updated_at": "2026-11-04T12:00:00Z",
        "html_url": URL,
        "user": {"login": "someone", "email": "x@example.invalid"},
        **extra,
    }


def _clock() -> datetime:
    return datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/org/repo/issues/1",
        "https://user:pass@github.com/org/repo/issues/1",
        "https://github.com/org/repo/issues/1?x=1",
        "https://github.com/org/repo/issues/1#frag",
        "https://github.example.com/org/repo/issues/1",
        "https://github.com:443/org/repo/issues/1",
        "https://github.com/org/repo/pull/1",
        "https://github.com/org/repo/issues/0",
        "https://github.com/org/repo/issues/1/../2",
    ],
)
def test_url_validation_rejects_host_credentials_and_query(url: str) -> None:
    with pytest.raises(IssueImportError):
        parse_issue_url(url)


def test_valid_url_is_parsed() -> None:
    ref = parse_issue_url(URL)
    assert (ref.repository, ref.number) == ("org/repo", 123)


def test_first_fifty_comments_are_deterministic() -> None:
    comments: list[dict[str, object]] = [
        {
            "id": 1000 - index,
            "body": f"comentário {index}",
            "created_at": f"2026-11-{1 + index // 24:02d}T{index % 24:02d}:00:00Z",
            "user": {"login": "someone"},
        }
        for index in range(60)
    ]
    reversed_gh = FakeGh(_issue(comments=75), list(reversed(comments)))
    ordered_gh = FakeGh(_issue(comments=75), comments)

    first = fetch_github_issue(URL, runner=reversed_gh, clock=_clock).content
    second = fetch_github_issue(URL, runner=ordered_gh, clock=_clock).content

    assert first == second
    assert first.comments_included == 50
    assert first.comments_total == 75
    assert first.comments_truncated is True
    assert first.comments[0].body == "comentário 0"
    assert all(call[call.index("--method") + 1] == "GET" for call in reversed_gh.calls)
    assert "per_page=50&page=1" in reversed_gh.calls[-1][-1]
    assert "someone" not in json.dumps(first.canonical_payload())


def test_issue_403_404_messages_are_actionable() -> None:
    def denied(args: Sequence[str], timeout: float) -> GhResult:
        return GhResult(1, b"", b"gh: Not Found (HTTP 404)\nraw secret token=abc")

    with pytest.raises(IssueImportError) as error:
        fetch_github_issue(URL, runner=denied)
    assert str(error.value) == (
        "Could not read issue (HTTP 404). Check 'gh auth status' and repository access."
    )
    assert "abc" not in str(error.value)


def test_pull_request_url_is_not_an_issue() -> None:
    with pytest.raises(IssueImportError, match="pull request"):
        fetch_github_issue(URL, runner=FakeGh(_issue(pull_request={}), []))


def test_offline_inputs_do_not_fabricate_github_identity() -> None:
    markdown = import_markdown("# Cadastro\n\nEmail opcional.\n", clock=_clock).content
    text = import_text("Email opcional no cadastro.", clock=_clock).content

    assert markdown.source_kind == SourceKind.MARKDOWN
    assert (markdown.repository, markdown.number, markdown.url) == (None, None, None)
    assert markdown.title == "Cadastro"
    assert text.source_kind == SourceKind.TEXT
    assert (text.repository, text.number, text.url) == (None, None, None)
    assert text.title == "Imported issue"
    assert text.labels == () and text.comments == ()


def test_declared_identity_stays_markdown() -> None:
    exported = "---\nurl: https://github.com/org/repo/issues/9\nlabels: [bug, ux]\n---\nCorpo\n"
    content = import_markdown(exported, clock=_clock).content
    assert content.source_kind == SourceKind.MARKDOWN
    assert (content.repository, content.number) == ("org/repo", 9)
    assert content.labels == ("bug", "ux")


def test_canonical_hash_excludes_capture_time() -> None:
    gh = FakeGh(_issue(), [])
    first = fetch_github_issue(URL, runner=gh, clock=_clock)
    later = fetch_github_issue(URL, runner=gh, clock=lambda: datetime(2026, 12, 1, tzinfo=UTC))
    assert first.capture_started_at != later.capture_started_at
    assert first.content.content_sha256 == later.content.content_sha256
    changed = fetch_github_issue(URL, runner=FakeGh(_issue(body="Outro corpo"), []))
    assert changed.content.content_sha256 != first.content.content_sha256


def test_redaction_happens_before_hash() -> None:
    capture = import_text("Use password=hunter2 para entrar.", clock=_clock)
    assert "hunter2" not in capture.content.body
    assert "[REDACTED]" in capture.content.body


def test_size_overflow_does_not_silently_drop_criteria() -> None:
    with pytest.raises(IssueImportError, match="not started"):
        import_text("x" * 100_001)
    big_comment = [{"id": 1, "body": "y" * 10_001, "created_at": "2026-11-04T00:00:00Z"}]
    with pytest.raises(IssueImportError):
        fetch_github_issue(URL, runner=FakeGh(_issue(comments=1), big_comment))


def test_issue_file_must_be_regular_utf8(tmp_path: Path) -> None:
    good = tmp_path / "issue.md"
    good.write_bytes(b"# T\r\nCorpo\r\n")
    assert read_issue_file(good) == "# T\nCorpo\n"
    bad = tmp_path / "bad.md"
    bad.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(IssueImportError, match="UTF-8"):
        read_issue_file(bad)
    with pytest.raises(IssueImportError, match="regular"):
        read_issue_file(tmp_path)


def test_requirements_projection_lists_comments() -> None:
    comments: list[dict[str, object]] = [
        {"id": 1, "body": "Também no mobile.", "created_at": "2026-11-04T00:00:00Z"}
    ]
    capture: IssueCapture = fetch_github_issue(
        URL, runner=FakeGh(_issue(comments=1), comments), clock=_clock
    )
    text = issue_requirements(capture.content)
    assert text.startswith("# Permitir email opcional")
    assert "Source: org/repo#123" in text
    assert "### Comment 1 (2026-11-04T00:00:00Z)\nTambém no mobile." in text
