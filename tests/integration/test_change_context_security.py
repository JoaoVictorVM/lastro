import os
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from lastro.change_context import EXCLUDED_SECRET, ComparisonMode, PreparedContext
from lastro.change_context.collector import Collector, CollectRequest
from lastro.change_context.git import Git
from lastro.change_context.scanner import Gitleaks
from lastro.change_context.source import SafeReader
from lastro.config.loader import ProjectContext
from lastro.config.source import SourceSettings
from lastro.history import TextMission, open_history

from .conftest import GitRepo

_OPENED: list[str] = []
_RECORDING: list[bool] = []


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if _RECORDING and event == "open" and args and isinstance(args[0], str | bytes | os.PathLike):
        _OPENED.append(os.fsdecode(args[0]))  # pyright: ignore[reportArgumentType]


sys.addaudithook(_audit)


@pytest.fixture
def opened() -> Iterator[list[str]]:
    """Rastro real de aberturas de arquivo feitas pelo processo durante a coleta."""
    _OPENED.clear()
    _RECORDING.append(True)
    try:
        yield _OPENED
    finally:
        _RECORDING.clear()


def aws_key() -> str:
    return "AKIA" + "IOSFODNN7" + "EXAMPLQ"


def collect(repo: GitRepo, allow: list[str], mode: ComparisonMode = ComparisonMode.WORKING_TREE,
            **kwargs: Any) -> Any:  # fmt: skip
    settings = SourceSettings(allow_paths=allow, base_ref="main")
    collector = Collector(settings, repo.root, scanner=Gitleaks(), **kwargs)
    return collector, collector.collect(CollectRequest(mode))


def make_link(link: Path, target: Path) -> str:
    """Symlink POSIX ou junction no Windows (não exige privilégio de symlink)."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
        return "reparse_point"
    link.symlink_to(target)
    return "symlink"


def test_denied_paths_are_not_opened_or_requested_as_blobs(
    git_repo: GitRepo, gitleaks: str, opened: list[str]
) -> None:
    git_repo.write(".env", "DB=prod\n")
    git_repo.write("config/id_rsa", "historic key\n")
    git_repo.commit("sensitive on main", ".env", "config/id_rsa")
    git_repo.git("rm", "-q", "config/id_rsa")
    git_repo.write(".env", "DB=staging\n")
    git_repo.commit("change sensitive files")
    git_repo.write("config/appsettings.Production.json", '{"ConnectionStrings": "x"}\n')
    git_repo.git("add", "config/appsettings.Production.json")
    git_repo.write(".env.local", "LOCAL=1\n")
    git_repo.write("src/form.py", "def save(email=None):\n    return email\n")
    denied_blobs = {
        line.split()[2]
        for ref in ("main", "HEAD")
        for line in git_repo.git("ls-tree", "-r", ref).splitlines()
        if not line.endswith(("src/form.py", "README.md"))
    }
    denied_blobs |= {git_repo.git("rev-parse", ":config/appsettings.Production.json")}
    _OPENED.clear()

    git = Git(git_repo.root)
    _, draft = collect(git_repo, ["**"], git=git)

    names = (".env", "id_rsa", "appsettings")
    assert not [path for path in opened if Path(path).name.startswith(names)]
    assert not denied_blobs & set(git.blob_requests)
    assert {source.relative_path for source in draft.sources} == {"src/form.py", "README.md"}
    reasons = {item.path: item.reason for item in draft.exclusions}
    assert reasons[".env"] == reasons[".env.local"] == "denied_pattern"
    assert reasons["config/appsettings.Production.json"] == "denied_pattern"
    assert "DB=" not in draft.diff_text and "historic key" not in draft.diff_text


def test_historical_sensitive_blob_is_never_requested(git_repo: GitRepo, gitleaks: str) -> None:
    git_repo.git("checkout", "-q", "main")
    git_repo.write("deploy/server.pem", "-----BEGIN CERT-----\n")
    git_repo.commit("old cert")
    blob = git_repo.git("rev-parse", "HEAD:deploy/server.pem")
    git_repo.git("checkout", "-q", "-b", "cleanup")
    git_repo.git("rm", "-q", "deploy/server.pem")
    git_repo.commit("remove cert")

    git = Git(git_repo.root)
    _, draft = collect(git_repo, ["**"], ComparisonMode.FEATURE, git=git)

    assert blob not in git.blob_requests
    assert ("deploy/server.pem", "denied_pattern") in {
        (item.path, item.reason) for item in draft.exclusions
    }


def test_external_symlink_is_denied_and_audited(
    git_repo: GitRepo,
    gitleaks: str,
    opened: list[str],
    tmp_path: Path,
    history_context: ProjectContext,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "stolen.py").write_text("STOLEN = 'outside data'\n", encoding="utf-8")
    reason = make_link(git_repo.root / "src" / "linked", outside)
    _OPENED.clear()

    _, draft = collect(git_repo, ["src/**"])

    assert not [path for path in opened if Path(path).name in ("outside", "stolen.py")]
    assert all(not source.relative_path.startswith("src/linked") for source in draft.sources)
    assert "outside data" not in draft.summary + draft.diff_text
    assert any(notice.reason == reason for notice in draft.security)
    history = open_history(history_context)
    created = history.create_run(
        history_context, TextMission("Explore"), prepared=PreparedContext(change=draft)
    )
    events = history.list_events(created.record.id)
    security: list[Any] = [
        event.payload for event in events if event.event_type == "security_event"
    ]
    assert security and security[0]["reason"] == reason


def test_reparse_swap_fails_closed(git_repo: GitRepo, gitleaks: str, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    (outside / "sub" / "x.py").write_text("SWAPPED = 'outside'\n", encoding="utf-8")
    git_repo.write("src/sub/x.py", "INSIDE = 1\n")

    class SwappingReader(SafeReader):
        swapped = False

        def read(self, relative: str, max_bytes: int = 1_000_000) -> bytes:
            if relative == "src/sub/x.py" and not self.swapped:
                self.swapped = True
                (git_repo.root / "src" / "sub").rename(git_repo.root / "src" / "sub_old")
                make_link(git_repo.root / "src" / "sub", outside / "sub")
            return super().read(relative, max_bytes)

    reader = SwappingReader(git_repo.root)
    _, draft = collect(git_repo, ["src/**"], reader=reader)

    assert reader.swapped
    assert "src/sub/x.py" not in {source.relative_path for source in draft.sources}
    assert "outside" not in draft.summary + draft.diff_text
    assert any(item.path == "src/sub/x.py" for item in draft.exclusions)


def test_appsettings_is_denied_before_read(
    git_repo: GitRepo, gitleaks: str, opened: list[str]
) -> None:
    git_repo.write("src/appsettings.json", "{}\n")
    git_repo.write("src/appsettings.Development.json", "{}\n")
    git_repo.commit("settings", "src/appsettings.json")
    _OPENED.clear()

    _, draft = collect(git_repo, ["src/**"])

    assert not [path for path in opened if Path(path).name.startswith("appsettings")]
    assert {item.path for item in draft.exclusions} >= {
        "src/appsettings.json",
        "src/appsettings.Development.json",
    }


def test_binary_and_oversized_candidates_are_excluded(git_repo: GitRepo, gitleaks: str) -> None:
    (git_repo.root / "src" / "logo.png").write_bytes(b"\x89PNG")
    (git_repo.root / "src" / "data.txt").write_bytes(b"text\x00more")
    (git_repo.root / "src" / "big.txt").write_bytes(b"a" * 1_000_001)

    _, draft = collect(git_repo, ["src/**"])

    reasons = {item.path: item.reason for item in draft.exclusions}
    assert reasons == {
        "src/logo.png": "binary",
        "src/data.txt": "binary",
        "src/big.txt": "oversized",
    }
    assert {source.relative_path for source in draft.sources} == {"src/form.py"}


def test_helpers_hooks_and_partial_clone_cannot_execute(
    git_repo: GitRepo, gitleaks: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "executed"
    script = tmp_path / "helper.sh"
    script.write_text(f"#!/bin/sh\necho ran >> '{marker.as_posix()}'\ncat\n", encoding="utf-8")
    script.chmod(0o755)
    hooks = git_repo.root / ".git" / "hooks"
    for name in ("reference-transaction", "post-index-change", "pre-auto-gc"):
        (hooks / name).write_text(script.read_text(), encoding="utf-8")
        (hooks / name).chmod(0o755)
    git_repo.git("config", "core.fsmonitor", script.as_posix())
    git_repo.git("config", "filter.evil.clean", f"sh '{script.as_posix()}'")
    git_repo.git("config", "filter.evil.smudge", f"sh '{script.as_posix()}'")
    git_repo.git("config", "diff.evil.textconv", f"sh '{script.as_posix()}'")
    git_repo.git("config", "diff.external", f"sh '{script.as_posix()}'")
    git_repo.git("config", "core.pager", f"sh '{script.as_posix()}'")
    git_repo.git("config", "remote.origin.promisor", "true")
    (git_repo.root / ".gitattributes").write_text("* filter=evil diff=evil\n", encoding="utf-8")
    git_repo.write("src/form.py", "def save(email=None):\n    return email\n")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", f"sh '{script.as_posix()}'")

    _, draft = collect(git_repo, ["src/**"])

    assert not marker.exists()
    assert "src/form.py" in {source.relative_path for source in draft.sources}


def test_path_secret_is_redacted(git_repo: GitRepo, gitleaks: str) -> None:
    git_repo.write("src/password=hunter2.py", "x = 1\n")
    _, draft = collect(git_repo, ["src/**"])
    rendered = str(draft.payload()) + draft.summary + draft.diff_text
    assert "hunter2" not in rendered
    assert ("src/password=[REDACTED]", "sensitive_path") in {
        (item.path, item.reason) for item in draft.exclusions
    }


def test_removed_line_secret_never_reaches_any_output(
    git_repo: GitRepo, gitleaks: str, history_context: ProjectContext
) -> None:
    secret = aws_key()
    git_repo.git("checkout", "-q", "main")
    git_repo.write("src/settings.py", f'AWS_KEY = "{secret}"\nTIMEOUT = 5\n')
    git_repo.commit("settings with key")
    git_repo.git("checkout", "-q", "-B", "feature/email")
    git_repo.write("src/settings.py", "TIMEOUT = 5\n")
    git_repo.commit("remove key")
    assert secret not in (git_repo.root / "src" / "settings.py").read_text(encoding="utf-8")

    _, draft = collect(git_repo, ["src/**"])
    history = open_history(history_context)
    created = history.create_run(
        history_context, TextMission("Explore"), prepared=PreparedContext(change=draft)
    )

    reasons = {item.path: item.reason for item in draft.exclusions}
    assert reasons["src/settings.py"] == EXCLUDED_SECRET
    assert "src/settings.py" not in {source.relative_path for source in draft.sources}
    assert f"[REDACTED] {EXCLUDED_SECRET}" in draft.diff_text
    assert secret not in draft.summary + draft.diff_text + str(draft.payload())
    for path in created.paths.run_dir.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), path
    with sqlite3.connect(history.database_path) as conn:
        conn.execute("PRAGMA wal_checkpoint(FULL)")
    for path in history.project_dir.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), path


def test_removed_line_secret_absent_from_cli_mcp_and_reports(
    git_repo: GitRepo,
    gitleaks: str,
    history_context: ProjectContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import json

    from typer.testing import CliRunner

    from lastro.browser.tools import BrowserTools
    from lastro.cli.app import app
    from lastro.history import RunStatus
    from lastro.reporting import ReportBuilder
    from lastro.reporting.public import export_public

    secret = aws_key()
    git_repo.git("checkout", "-q", "main")
    git_repo.write("src/settings.py", f'AWS_KEY = "{secret}"\nTIMEOUT = 5\n')
    git_repo.commit("settings with key")
    git_repo.git("checkout", "-q", "-B", "feature/email")
    git_repo.write("src/settings.py", "TIMEOUT = 5\n")
    git_repo.commit("remove key")
    config = history_context.config_path
    config.write_text(
        config.read_text(encoding="utf-8")
        + '\n[source]\nallow_paths = ["src/**"]\nbase_ref = "main"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("SOURCE_REPO_PATH", str(git_repo.root))

    cli = CliRunner().invoke(
        app, ["--profile", "homolog", "run", "start", "--mission", "Explore", "--source"]
    )
    assert cli.exit_code == 0, cli.output
    history = open_history(history_context)
    run = history.list_runs()[0].record
    tools = BrowserTools(history_context, history, run.id)
    responses = [
        asyncio.run(tools.call("mission_context", {})),
        asyncio.run(tools.call("mission_context", {"section": "changes"})),
    ]
    assert EXCLUDED_SECRET in responses[1]["page"]["content"]
    history.finish_run(run.id, RunStatus.COMPLETED)
    ReportBuilder(history).build_final(run.id)
    export_public(history, run.id)

    outputs = cli.output + cli.stderr + json.dumps(responses, ensure_ascii=False)
    assert secret not in outputs
    with sqlite3.connect(history.database_path) as conn:
        conn.execute("PRAGMA wal_checkpoint(FULL)")
    for path in history.project_dir.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), path
