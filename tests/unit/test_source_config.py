from pathlib import Path

import pytest
from pydantic import ValidationError

from lastro.config.source import (
    MAX_CANDIDATES,
    SourceConfigError,
    SourceSettings,
    is_valid_ref,
    resolve_source_root,
)


def test_source_is_opt_in() -> None:
    settings = SourceSettings()
    assert settings.enabled is False
    assert settings.repo_path_env == "SOURCE_REPO_PATH"
    assert settings.base_ref is None


def test_empty_allowlist_rejects_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCE_REPO_PATH", str(tmp_path))
    with pytest.raises(SourceConfigError, match="allow_paths"):
        resolve_source_root(SourceSettings())
    assert resolve_source_root(SourceSettings(allow_paths=["src/**"])) == tmp_path


def test_missing_or_relative_root_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SourceSettings(allow_paths=["src/**"])
    monkeypatch.delenv("SOURCE_REPO_PATH", raising=False)
    with pytest.raises(SourceConfigError, match="SOURCE_REPO_PATH"):
        resolve_source_root(settings)
    monkeypatch.setenv("SOURCE_REPO_PATH", "relative/repo")
    with pytest.raises(SourceConfigError, match="absolute"):
        resolve_source_root(settings)


def test_only_lower_limits_are_accepted() -> None:
    assert SourceSettings(max_candidates=10).max_candidates == 10
    with pytest.raises(ValidationError):
        SourceSettings(max_candidates=MAX_CANDIDATES + 1)
    with pytest.raises(ValidationError):
        SourceSettings(max_total_bytes=20_000_001)


@pytest.mark.parametrize(
    "pattern", ["/etc/**", "../x", "src/../x", "C:/x", "src\\x", "a:b", "", "src//x"]
)
def test_unsafe_globs_are_rejected(pattern: str) -> None:
    with pytest.raises(ValidationError):
        SourceSettings(allow_paths=[pattern])


@pytest.mark.parametrize(
    ("ref", "valid"),
    [
        ("origin/main", True),
        ("main", True),
        ("-main", False),
        ("main~1", False),
        ("HEAD^", False),
        ("a..b", False),
        ("main@{1}", False),
        ("x:y", False),
        ("refs/heads/x.lock", False),
    ],
)
def test_refs_reject_revision_syntax(ref: str, valid: bool) -> None:
    assert is_valid_ref(ref) is valid


def test_deny_precedes_allow() -> None:
    from lastro.change_context.source import DENIED_PATTERN, NOT_ALLOWED, SourcePolicy

    policy = SourcePolicy(("**",))
    assert policy.classify(".env") == DENIED_PATTERN
    assert policy.classify("config/.ENV.production") == DENIED_PATTERN
    assert policy.classify("a/.git/config") == DENIED_PATTERN
    assert policy.classify("src/App.py") is None
    strict = SourcePolicy(("src/*.py",))
    assert strict.classify("src/App.py") is None
    assert strict.classify("SRC/App.py") == NOT_ALLOWED
    assert strict.classify("src/sub/App.py") == NOT_ALLOWED
