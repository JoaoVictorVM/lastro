import hashlib
import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from lastro.change_context import ComparisonMode, Layer, RemoteFreshness, SourceState
from lastro.change_context.collector import NOT_GIT_REPOSITORY, Collector, CollectRequest
from lastro.change_context.git import Git
from lastro.change_context.issues import GhResult
from lastro.change_context.scanner import Gitleaks
from lastro.config.source import SourceSettings

from .conftest import GitRepo

ALLOW = ["src/**", "README.md", "docs/**"]


def collector(repo: GitRepo, *, git: Git | None = None, **kwargs: Any) -> Collector:
    settings = SourceSettings(allow_paths=ALLOW, base_ref=kwargs.pop("base_ref", "main"))
    return Collector(settings, repo.root, scanner=Gitleaks(), git=git, **kwargs)


def snapshot_state(repo: GitRepo) -> dict[str, Any]:
    files: dict[str, str] = {}
    for path in sorted(repo.root.rglob("*")):
        if ".git" in path.relative_to(repo.root).parts or not path.is_file():
            continue
        files[path.relative_to(repo.root).as_posix()] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    index = repo.root / ".git" / "index"
    return {
        "status": repo.git("status", "--porcelain=v1", "--untracked-files=all"),
        "branch": repo.git("rev-parse", "--abbrev-ref", "HEAD"),
        "head": repo.git("rev-parse", "HEAD"),
        "index": hashlib.sha256(index.read_bytes()).hexdigest(),
        "files": files,
        "fetch_head": (repo.root / ".git" / "FETCH_HEAD").exists(),
    }


def dirty_feature(repo: GitRepo) -> None:
    repo.write("src/form.py", "def save(email=None):\n    return email\n")
    repo.commit("email opcional")
    repo.write("src/validation.py", "REQUIRED = ('name',)\n")
    repo.git("add", "src/validation.py")
    repo.write("README.md", "# demo\n\nEmail opcional.\n")
    repo.write("src/new_view.py", "VIEW = 'cadastro'\n")


def test_collection_preserves_branch_index_and_worktree(git_repo: GitRepo, gitleaks: str) -> None:
    dirty_feature(git_repo)
    before = snapshot_state(git_repo)
    assert before["status"]

    draft = collector(git_repo).collect(CollectRequest(ComparisonMode.WORKING_TREE))

    assert snapshot_state(git_repo) == before
    assert draft.source_state == SourceState.AVAILABLE
    assert draft.dirty is True
    assert draft.branch == "feature/email"


def test_staged_unstaged_untracked_layers(git_repo: GitRepo, gitleaks: str) -> None:
    dirty_feature(git_repo)

    draft = collector(git_repo).collect(CollectRequest(ComparisonMode.WORKING_TREE))

    layers = {(item.path, item.layer) for item in draft.files}
    assert ("src/form.py", Layer.COMMITTED) in layers
    assert ("src/validation.py", Layer.STAGED) in layers
    assert ("README.md", Layer.UNSTAGED) in layers
    assert ("src/new_view.py", Layer.UNTRACKED) in layers
    assert draft.merge_base == git_repo.git("rev-parse", "main")
    assert "+Email opcional." in draft.diff_text
    paths = {source.relative_path for source in draft.sources}
    assert {"src/form.py", "src/validation.py", "README.md", "src/new_view.py"} <= paths
    by_path = {item.path: item for item in draft.files}
    assert by_path["src/form.py"].file_id in {source.file_id for source in draft.sources}


def test_feature_mode_only_reports_commits(git_repo: GitRepo, gitleaks: str) -> None:
    dirty_feature(git_repo)
    draft = collector(git_repo).collect(CollectRequest(ComparisonMode.FEATURE))
    assert {item.layer for item in draft.files} == {Layer.COMMITTED}
    assert draft.dirty is False
    assert all(source.revision_oid for source in draft.sources)


def test_detached_head(git_repo: GitRepo, gitleaks: str) -> None:
    git_repo.write("src/form.py", "x = 1\n")
    head = git_repo.commit("change")
    git_repo.git("checkout", "-q", "--detach")

    draft = collector(git_repo).collect(CollectRequest(ComparisonMode.FEATURE))

    assert draft.branch is None
    assert draft.head == head
    assert draft.analyzed_code_version == head


def test_missing_base_has_no_silent_fallback(git_repo: GitRepo, gitleaks: str) -> None:
    git_repo.write("src/form.py", "x = 1\n")
    git_repo.commit("change")

    draft = collector(git_repo, base_ref="origin/missing").collect(
        CollectRequest(ComparisonMode.FEATURE)
    )

    assert draft.base_commit is None and draft.merge_base is None
    assert draft.files == ()
    assert draft.source_state == SourceState.PARTIAL
    assert any("comparison unavailable" in item for item in draft.warnings)


def test_no_implicit_network_or_fetch(git_repo: GitRepo, gitleaks: str) -> None:
    git_repo.git("remote", "add", "origin", "https://127.0.0.1:9/org/repo.git")
    draft = collector(git_repo).collect(CollectRequest(ComparisonMode.WORKING_TREE))
    assert draft.remote_freshness == RemoteFreshness.NOT_CHECKED
    assert not (git_repo.root / ".git" / "FETCH_HEAD").exists()
    assert draft.origin_identity == "127.0.0.1/org/repo"


def test_fetch_only_updates_git_metadata(git_repo: GitRepo, gitleaks: str, tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    git_repo.git("remote", "add", "origin", str(remote))
    git_repo.git("push", "-q", "origin", "main")
    other = GitRepo(tmp_path / "other")
    subprocess.run(["git", "clone", "-q", str(remote), str(other.root)], check=True)
    other.write("docs/new.md", "nova base\n")
    upstream = other.commit("upstream")
    other.git("push", "-q", "origin", "HEAD:main")
    git_repo.write("src/form.py", "y = 2\n")
    before = snapshot_state(git_repo)

    # Transporte local só para o teste; em produção apenas HTTPS e SSH são aceitos.
    git = Git(git_repo.root, allowed_protocols=("file",))
    draft = collector(git_repo, git=git, base_ref="origin/main").collect(
        CollectRequest(ComparisonMode.WORKING_TREE, fetch=True)
    )

    after = snapshot_state(git_repo)
    assert draft.remote_freshness == RemoteFreshness.FETCHED
    assert draft.base_commit == upstream
    assert git_repo.git("rev-parse", "origin/main") == upstream
    for key in ("status", "branch", "head", "index", "files"):
        assert after[key] == before[key]


def test_fetch_refuses_unsupported_transport(
    git_repo: GitRepo, gitleaks: str, tmp_path: Path
) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    git_repo.git("remote", "add", "origin", str(remote))
    draft = collector(git_repo).collect(CollectRequest(ComparisonMode.WORKING_TREE, fetch=True))
    assert draft.remote_freshness == RemoteFreshness.FETCH_FAILED
    assert not (git_repo.root / ".git" / "FETCH_HEAD").exists()


def test_not_git_repository_is_skipped(tmp_path: Path, gitleaks: str) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    settings = SourceSettings(allow_paths=ALLOW)
    draft = Collector(settings, plain, scanner=Gitleaks()).collect(CollectRequest())
    assert draft.source_state == SourceState.SKIPPED
    assert draft.warnings == (NOT_GIT_REPOSITORY,)


def _pr_runner(head_sha: str, head_repo: str, base_sha: str) -> Any:
    def run(args: Sequence[str], timeout: float) -> GhResult:
        data = {
            "base": {"sha": base_sha, "repo": {"full_name": "org/repo"}},
            "head": {"sha": head_sha, "repo": {"full_name": head_repo}},
        }
        return GhResult(0, json.dumps(data).encode(), b"")

    return run


def test_pr_fork_missing_objects_is_unavailable(git_repo: GitRepo, gitleaks: str) -> None:
    git_repo.git("remote", "add", "origin", "https://github.com/org/repo.git")
    base = git_repo.git("rev-parse", "main")
    missing = "c" * 40

    draft = collector(git_repo, gh_runner=_pr_runner(missing, "fork/repo", base)).collect(
        CollectRequest(ComparisonMode.PR, pr_url="https://github.com/org/repo/pull/5")
    )

    assert draft.files == () and draft.sources == ()
    assert draft.pr is not None and draft.pr["fork"] is True
    assert draft.analyzed_code_version == missing
    assert any("objects missing" in item for item in draft.warnings)


def test_pr_with_local_objects_compares_merge_base(git_repo: GitRepo, gitleaks: str) -> None:
    git_repo.git("remote", "add", "origin", "git@github.com:org/repo.git")
    base = git_repo.git("rev-parse", "main")
    git_repo.write("src/form.py", "def save(email=None):\n    return email\n")
    head = git_repo.commit("pr change")
    git_repo.git("checkout", "-q", "main")
    git_repo.write("src/local_only.py", "LOCAL = 1\n")

    draft = collector(git_repo, gh_runner=_pr_runner(head, "org/repo", base)).collect(
        CollectRequest(ComparisonMode.PR, pr_url="https://github.com/org/repo/pull/5")
    )

    assert [(item.path, item.layer) for item in draft.files] == [("src/form.py", Layer.COMMITTED)]
    assert draft.analyzed_code_version == head
    assert "src/local_only.py" not in {source.relative_path for source in draft.sources}


def test_last_passed_uses_explicit_baseline(git_repo: GitRepo, gitleaks: str) -> None:
    passed = git_repo.git("rev-parse", "HEAD")
    git_repo.write("src/form.py", "z = 3\n")
    git_repo.commit("after baseline")

    draft = collector(git_repo, baseline=lambda flow: ("run_x", passed)).collect(
        CollectRequest(ComparisonMode.LAST_PASSED, flow_key="cadastro")
    )
    unavailable = collector(git_repo, baseline=lambda flow: None).collect(
        CollectRequest(ComparisonMode.LAST_PASSED, flow_key="cadastro")
    )

    assert draft.base_commit == passed and draft.baseline_run_id == "run_x"
    assert [item.path for item in draft.files] == ["src/form.py"]
    assert unavailable.files == ()
    assert any("no eligible last-passed baseline" in item for item in unavailable.warnings)


@pytest.mark.parametrize("limit", [1])
def test_limits_mark_partial_result(git_repo: GitRepo, gitleaks: str, limit: int) -> None:
    git_repo.write("src/a.py", "a = 1\n")
    git_repo.write("src/b.py", "b = 1\n")
    git_repo.commit("two files")
    settings = SourceSettings(allow_paths=ALLOW, base_ref="main", max_candidates=limit)
    draft = Collector(settings, git_repo.root, scanner=Gitleaks()).collect(
        CollectRequest(ComparisonMode.FEATURE)
    )
    assert draft.truncated is True
    assert draft.source_state == SourceState.PARTIAL
    assert len(draft.sources) == limit
    assert "coverage is not complete" in draft.summary
