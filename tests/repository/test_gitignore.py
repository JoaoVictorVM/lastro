import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

GitRunner = Callable[..., subprocess.CompletedProcess[str]]

REPO_ROOT = Path(__file__).resolve().parents[2]

# One sample per sensitive pattern of the ignore policy, grouped as in .gitignore.
SENSITIVE_SAMPLES: tuple[str, ...] = (
    # Secrets and environment
    ".env",
    ".env.local",
    "config/.env.production",
    "keys/server.pem",
    "keys/server.key",
    "keys/client.p12",
    "keys/client.pfx",
    "keys/server.crt",
    "tests/fixtures/other/ca.crt",
    "id_rsa",
    "id_rsa.pub",
    "id_ed25519",
    "credentials.json",
    "credentials-homolog.json",
    "secrets.toml",
    "api.secret",
    # Browser session material
    "storage_state.json",
    "storage_state-aurora.json",
    ".auth/state.json",
    "src/lastro/.auth/state.json",
    "auth/cookies.json",
    "somewhere/auth/cookies.json",
    "captures/session.har",
    "trace.zip",
    "runs-export/login.trace.zip",
    "test-results/output.txt",
    "playwright-report/index.html",
    ".playwright-mcp/console.log.txt",
    # Lastro private data inside a repository
    ".lastro/config.txt",
    "lastro-data/run.txt",
    "runs/run-1/events.jsonl",
    "evidence/finding-1.txt",
    "registry.sqlite",
    "registry.sqlite3",
    "registry.db",
    "registry.db-journal",
    "registry.db-wal",
    "registry.db-shm",
    "registry.sqlite-wal",
    "registry.sqlite-shm",
    # Evidence media outside public docs
    "recording.webm",
    "recording.mp4",
    "screenshot.png",
    "reports/nested/step-3.png",
    "demo/static/logo.png",
    # Imported content and indexes
    "issue-snapshots/123.json",
    "source-cache/app.py.txt",
    "index/chunks.json",
    "somewhere/index/chunks.json",
    "vectors.faiss",
    "embeddings.vec",
    # Evaluation outputs
    "evals/results/private/run-1.json",
    # Executor local settings
    ".claude/settings.local.json",
    "CLAUDE.local.md",
    ".codex/state.json",
    ".mcp.local.json",
    # Observability local stack
    ".langfuse/data.txt",
    "docker-data/volume.txt",
    "langfuse.env",
    # Logs
    "lastro.log",
    "logs/run.txt",
    # Python and tooling
    "src/pkg/__pycache__/mod.cpython-312.pyc",
    "mod.pyc",
    "mod.pyo",
    "mod.pyd",
    ".venv/pyvenv.cfg",
    "venv/pyvenv.cfg",
    "dist/lastro-0.1.0.tar.gz",
    "build/lib/module.txt",
    "lastro.egg-info/PKG-INFO",
    ".pytest_cache/README.md",
    ".ruff_cache/CACHEDIR.TAG",
    ".mypy_cache/CACHEDIR.TAG",
    ".pyright/cache.json",
    ".coverage",
    ".coverage.host.1234",
    "coverage.xml",
    "htmlcov/index.html",
    # Editors and OS
    ".idea/workspace.xml",
    ".vscode/settings.json",
    "notes.md.swp",
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
    # Sensitive files stay ignored inside the public exception folders
    "docs/assets/.env",
    "docs/assets/deploy.key",
    "src/lastro_demo/static/server.pem",
    "evals/results/public/.env",
    "src/lastro/auth/.env",
    "src/lastro/auth/session.key",
    "src/lastro/auth/storage_state.json",
    "src/lastro/index/registry.db",
    "src/lastro/index/vectors.faiss",
)

PUBLIC_SAMPLES: tuple[str, ...] = (
    ".env.example",
    "src/lastro_demo/.env.example",
    "tests/fixtures/certs/ca.crt",
    "tests/fixtures/certs/nested/server.pem",
    "docs/assets/demo.gif",
    "docs/assets/screenshot.png",
    "src/lastro_demo/static/app.css",
    "src/lastro_demo/static/htmx.min.js",
    "src/lastro_demo/static/logo.png",
    "evals/results/public/summary.json",
    "evals/results/public/chart.png",
    ".vscode/extensions.json",
    "src/lastro/auth/x.py",
    "src/lastro/index/x.py",
)


@pytest.fixture(scope="module")
def ignored(
    tmp_path_factory: pytest.TempPathFactory,
    make_repo: Callable[[Path], Path],
    git: GitRunner,
) -> frozenset[str]:
    repo = make_repo(tmp_path_factory.mktemp("ignore-policy"))
    shutil.copyfile(REPO_ROOT / ".gitignore", repo / ".gitignore")
    samples = (*SENSITIVE_SAMPLES, *PUBLIC_SAMPLES)
    for sample in samples:
        path = repo / sample
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("sample\n", encoding="utf-8")
    # NUL separators keep paths intact on Windows, where text mode would append a CR to each line.
    result = git("check-ignore", "--stdin", "-z", cwd=repo, stdin="\0".join(samples) + "\0")
    # Exit code 1 means "nothing ignored"; anything above it is a Git error.
    assert result.returncode in (0, 1), result.stderr
    return frozenset(path for path in result.stdout.split("\0") if path)


def not_ignored_message(paths: list[str]) -> str:
    return "\n".join(f"Sensitive path not ignored: {path}" for path in paths)


def test_sensitive_samples_are_ignored(ignored: frozenset[str]) -> None:
    missing = [sample for sample in SENSITIVE_SAMPLES if sample not in ignored]

    assert not missing, not_ignored_message(missing)


def test_env_examples_are_not_ignored(ignored: frozenset[str]) -> None:
    assert ".env.example" not in ignored
    assert "src/lastro_demo/.env.example" not in ignored
    assert (REPO_ROOT / ".env.example").is_file()
    assert (REPO_ROOT / "src/lastro_demo/.env.example").is_file()


def test_certificate_fixtures_are_not_ignored(ignored: frozenset[str]) -> None:
    assert "tests/fixtures/certs/ca.crt" not in ignored
    assert "tests/fixtures/certs/nested/server.pem" not in ignored
    assert "tests/fixtures/other/ca.crt" in ignored
    assert "keys/server.crt" in ignored


def test_public_assets_are_not_ignored(ignored: frozenset[str]) -> None:
    assert "docs/assets/demo.gif" not in ignored
    assert "docs/assets/screenshot.png" not in ignored
    assert "screenshot.png" in ignored
    assert "reports/nested/step-3.png" in ignored


def test_demo_static_exception_follows_resolved_path(ignored: frozenset[str]) -> None:
    for sample in (
        "src/lastro_demo/static/app.css",
        "src/lastro_demo/static/htmx.min.js",
        "src/lastro_demo/static/logo.png",
    ):
        assert sample not in ignored, sample
    assert "demo/static/logo.png" in ignored
    assert not (REPO_ROOT / "demo").exists()
    assert (REPO_ROOT / "src/lastro_demo/static/htmx.min.js").is_file()


def test_public_eval_results_are_not_ignored(ignored: frozenset[str]) -> None:
    assert "evals/results/public/summary.json" not in ignored
    assert "evals/results/public/chart.png" not in ignored
    assert "evals/results/private/run-1.json" in ignored


def test_vscode_extensions_is_not_ignored(ignored: frozenset[str]) -> None:
    assert ".vscode/extensions.json" not in ignored
    assert ".vscode/settings.json" in ignored


def test_source_packages_named_auth_or_index_are_not_ignored(ignored: frozenset[str]) -> None:
    assert "src/lastro/auth/x.py" not in ignored
    assert "src/lastro/index/x.py" not in ignored
    for sample in (
        "auth/cookies.json",
        "somewhere/auth/cookies.json",
        "index/chunks.json",
        "somewhere/index/chunks.json",
        ".auth/state.json",
        "src/lastro/.auth/state.json",
        "src/lastro/auth/.env",
        "src/lastro/index/registry.db",
    ):
        assert sample in ignored, sample


def test_planning_artifacts_remain_local() -> None:
    patterns = [
        line.strip()
        for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]

    assert not any(".planning" in pattern for pattern in patterns)


def test_tracked_files_are_not_ignored(git: GitRunner) -> None:
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not running from a Git checkout")
    result = git("ls-files", "--cached", "--ignored", "--exclude-standard", cwd=REPO_ROOT)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_real_checkout_ignores_env(git: GitRunner) -> None:
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not running from a Git checkout")
    result = git("check-ignore", "--no-index", "-q", ".env", cwd=REPO_ROOT)

    assert result.returncode == 0, not_ignored_message([".env"])
