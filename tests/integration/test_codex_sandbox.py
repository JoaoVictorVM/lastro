"""Probes com o Codex real: opt-in, pois alguns consomem a cota da assinatura.

LASTRO_CODEX_PROBES=sandbox roda só o que não gasta inferência; =inference inclui uma sessão real.
Sem a variável, tudo é pulado e o isolamento NÃO fica comprovado.
"""

import http.server
import os
import shutil
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from lastro.codex import config as cc
from lastro.codex.launcher import _instructions, launch
from lastro.codex.models import LaunchMode
from lastro.codex.preflight import codex_home, default_runner, probe_install

from .codex_support import CodexProject, make_project

LEVEL = os.environ.get("LASTRO_CODEX_PROBES", "")
needs_probe = pytest.mark.skipif(
    LEVEL not in {"sandbox", "inference"} or shutil.which("codex") is None,
    reason="set LASTRO_CODEX_PROBES=sandbox with an installed and logged-in Codex",
)
needs_inference = pytest.mark.skipif(
    LEVEL != "inference", reason="set LASTRO_CODEX_PROBES=inference; this consumes account quota"
)


@pytest.fixture
def real_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[CodexProject]:
    # O conftest isola HOME; o Codex real precisa do home verdadeiro para achar o login.
    real_home = os.environ.get("LASTRO_CODEX_REAL_HOME") or str(Path(os.path.expanduser("~")))
    monkeypatch.setenv("CODEX_HOME", os.environ.get("CODEX_HOME", str(Path(real_home) / ".codex")))
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    (project.directory / ".auth").mkdir()
    (project.directory / ".auth" / "s.json").write_text("canary", encoding="utf-8")
    catalog = project.directory / "evals" / "catalog"
    catalog.mkdir(parents=True)
    (catalog / "c.json").write_text("canary", encoding="utf-8")
    nested = project.directory / "sub" / "deep"
    nested.mkdir(parents=True)
    (nested / ".env.local").write_text("canary", encoding="utf-8")
    yield project


def _identity(project: CodexProject) -> cc.SessionIdentity:
    context = project.context()
    return cc.SessionIdentity(
        project_dir=project.directory,
        config_path=context.config_path,
        profile=context.profile_name,
        run_id="run_probe",
        private_root=context.loaded.private_data_root,
        codex_home=codex_home(),
        instructions=_instructions(project.directory),
    )


def _read_probe(target: Path) -> str:
    return (
        "import sys\n"
        "try:\n"
        f"    open(r'{target}','rb')\n"
        "except PermissionError:\n"
        "    sys.exit(0)\n"
        "sys.exit(3)"
    )


def _sandboxed(project: CodexProject, *command: str) -> int:
    install = probe_install()
    policy = cc.load_policy(project.directory)
    overrides = cc.session_overrides(policy, _identity(project), [])
    arguments = [
        *install.command,
        "sandbox",
        *overrides,
        "--permission-profile",
        cc.PROFILE_NAME,
        "--cd",
        str(project.directory),
        "--",
        *command,
    ]
    done = default_runner(arguments)
    for _ in range(cc.SELF_TEST_ATTEMPTS - 1):
        if done.returncode in {0, 3}:
            break
        time.sleep(cc.SELF_TEST_RETRY_SECONDS)
        done = default_runner(arguments)
    print(done.stdout, done.stderr)
    return done.returncode


@needs_probe
@pytest.mark.parametrize(
    "relative", [".env", ".auth/s.json", "evals/catalog/c.json", "sub/deep/.env.local"]
)
def test_real_codex_denies_sensitive_reads(real_project: CodexProject, relative: str) -> None:
    code = _read_probe(real_project.directory / relative)
    assert _sandboxed(real_project, sys.executable, "-I", "-c", code) == 0


@needs_probe
def test_real_codex_denies_file_created_after_start(real_project: CodexProject) -> None:
    late = real_project.directory / "late" / ".env"
    late.parent.mkdir()
    late.write_text("canary", encoding="utf-8")
    code = _read_probe(late)
    assert _sandboxed(real_project, sys.executable, "-I", "-c", code) == 0


@needs_probe
def test_real_codex_denies_direct_network(real_project: CodexProject) -> None:
    code = (
        "import socket,sys\n"
        "try:\n"
        "    socket.create_connection(('192.0.2.1',9),3).close()\n"
        "except PermissionError:\n"
        "    sys.exit(0)\n"
        "except OSError:\n"
        "    pass\n"
        "sys.exit(3)"
    )
    assert _sandboxed(real_project, sys.executable, "-I", "-c", code) == 0


@needs_probe
@pytest.mark.xfail(
    sys.platform == "win32",
    reason="the Windows sandbox allows connections to this host; no agent tool can open a socket",
)
def test_real_codex_denies_local_network(real_project: CodexProject) -> None:
    hits: list[str] = []

    class Canary(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Canary)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        code = (
            "import socket,sys\n"
            "try:\n"
            f"    socket.create_connection(('127.0.0.1',{port}),3).close()\n"
            "except OSError:\n"
            "    sys.exit(0)\n"
            "sys.exit(3)"
        )
        assert _sandboxed(real_project, sys.executable, "-I", "-c", code) == 0
    finally:
        server.shutdown()
    assert hits == []


@needs_probe
def test_real_codex_catalog_is_lastro_only(real_project: CodexProject) -> None:
    install = probe_install()
    policy = cc.load_policy(real_project.directory)
    identity = _identity(real_project)
    foreign = cc.resolve_foreign_servers(install.command, policy, identity, default_runner)
    overrides = cc.session_overrides(policy, identity, foreign)
    cc.verify_effective_tools(install.command, overrides, default_runner)
    cc.sandbox_self_test(install.command, overrides, identity, default_runner)


@needs_probe
@needs_inference
def test_real_codex_demo_mission(real_project: CodexProject) -> None:
    """Sessão real: comprova login, MCP obrigatório e encerramento durável pelas tools Lastro."""
    run_id = real_project.create_run()
    result = launch(
        real_project.context(),
        real_project.history(),
        run_id,
        LaunchMode.HEADLESS,
        write=print,
    )
    assert result.executor == "codex"
    assert result.status in {"completed", "interrupted", "blocked"}
    assert result.report_path is not None


@needs_probe
@needs_inference
def test_real_codex_interactive_lifecycle() -> None:
    pytest.skip("exige um terminal real; execute lastro run launch manualmente e confira a saída")
