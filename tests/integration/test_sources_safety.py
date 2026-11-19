import os
import sqlite3
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest
from tests.integration.sources_support import (
    CADASTRO,
    BrokenScanner,
    MarkerScanner,
    make_pdf,
    make_service,
    write_docs,
)

from lastro.change_context import source as code_source
from lastro.change_context.source import BINARY, SourcePolicy
from lastro.config.loader import ProjectContext
from lastro.history import HistoryStore, open_history
from lastro.history.database import Database
from lastro.sources import extraction, retrieval
from lastro.sources.models import (
    EXTRACTION_TIMEOUT,
    RESOURCE_LIMIT,
    DocumentFormat,
    ExtractionFailure,
    SourcesError,
)
from lastro.sources.safety import inventory

pytest.importorskip("sqlite_vec")
pytest.importorskip("markdown_it")
pytest.importorskip("pypdf")


@pytest.fixture
def store(history_context: ProjectContext, fake_clock: Callable[[], datetime]) -> HistoryStore:
    return open_history(history_context, clock=fake_clock)


def _link(link: Path, target: Path) -> None:
    if sys.platform == "win32" and target.is_dir():
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
        return
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError:
        pytest.skip("symlinks indisponíveis neste ambiente")


def _all_rows(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%fts_%'"
            )
        ]
        return "\n".join(
            repr(conn.execute(f'SELECT * FROM "{table}"').fetchall()) for table in tables
        )
    finally:
        conn.close()


def test_symlink_junction_escape_and_toctou(
    store: HistoryStore, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = write_docs(workdir / "outside", {"secreto.md": "# Fora\n\nconteudo externo"})
    docs = write_docs(workdir / "docs", {"ok.md": "# Ok\n\ninterno", "credentials.md": "x"})
    _link(docs / "pasta", outside)

    listing = inventory(docs)

    assert [item.relative_path for item in listing.candidates] == ["ok.md"]
    reasons = {item.relative_path: item.reason for item in listing.exclusions}
    assert reasons == {"credentials.md": "denied_pattern", "pasta": "unsafe_path"}

    target = docs / "ok.md"
    original = code_source.SafeReader._drain

    def growing(handle: int, max_bytes: int) -> bytes:
        with target.open("a", encoding="utf-8") as stream:
            stream.write(" mudou")
        return original(handle, max_bytes)

    monkeypatch.setattr(code_source.SafeReader, "_drain", staticmethod(growing))
    result = make_service(store).add(docs, "work")

    assert result.reasons == {
        "denied_pattern": 1,
        "unsafe_path": 1,
        "unstable_file": 1,
    }
    assert "conteudo externo" not in _all_rows(store.database_path)


def test_secret_pdf_and_text_never_persist(store: HistoryStore, workdir: Path) -> None:
    marker = MarkerScanner.MARKER
    docs = write_docs(
        workdir / "docs",
        {"limpo.md": CADASTRO, "nota.txt": "versao limpa da nota"},
    )
    service = make_service(store, scanner=MarkerScanner())
    service.add(docs, "work")
    leaked_pdf = make_pdf([f"chave {marker} aqui"])
    write_docs(docs, {"nota.txt": f"agora tem {marker}", "vazado.pdf": leaked_pdf})

    result = service.sync()

    assert result.reasons == {"possible_secret": 2}
    rows = _all_rows(store.database_path)
    assert marker not in rows
    import hashlib

    for content in (f"agora tem {marker}".encode(), leaked_pdf):
        assert hashlib.sha256(content).hexdigest() not in rows
    outcome = service.query(run_id=None, flow="f", text="versao limpa nota", limit=5)
    assert all("nota.txt" not in str(item.locator["path"]) for item in outcome.passages)


def test_code_policy_still_denies_pdf() -> None:
    policy = SourcePolicy(("**",))

    assert policy.classify_entry("docs/manual.pdf", "100644", 10) == BINARY


def test_scanner_failure_preserves_generation(store: HistoryStore, workdir: Path) -> None:
    docs = write_docs(workdir / "docs", {"a.md": "# A\n\nprimeira versao"})
    make_service(store).add(docs, "work")
    before = store.sources.active()
    write_docs(docs, {"a.md": "# A\n\nsegunda versao"})
    broken = make_service(store, scanner=BrokenScanner())

    with pytest.raises(SourcesError) as caught:
        broken.sync()

    assert caught.value.code == "scanner_unavailable"
    assert store.sources.active() == before
    outcome = make_service(store).query(run_id=None, flow="f", text="primeira versao", limit=5)
    assert outcome.passages and "primeira" in outcome.passages[0].text


def _script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "worker.py"
    path.write_text("import json, sys, time\nsys.stdin.buffer.read()\n" + body, encoding="utf-8")
    return path


def test_pdf_timeout_and_memory_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    slow = _script(tmp_path, "time.sleep(30)\n")
    monkeypatch.setattr(extraction, "worker_command", lambda: [sys.executable, "-I", str(slow)])
    monkeypatch.setattr(extraction, "EXTRACTION_TIMEOUT_SECONDS", 1.0)
    with pytest.raises(ExtractionFailure) as timeout:
        extraction.extract(DocumentFormat.PDF, b"%PDF", "a.pdf")
    assert timeout.value.reason == EXTRACTION_TIMEOUT

    hungry = _script(
        tmp_path,
        "try:\n"
        "    data = bytearray(1024 * 1024 * 1024)\n"
        "    print(json.dumps({'pages': ['sem limite']}))\n"
        "except MemoryError:\n"
        "    print(json.dumps({'error': 'resource_limit'}))\n",
    )
    monkeypatch.setattr(extraction, "worker_command", lambda: [sys.executable, "-I", str(hungry)])
    monkeypatch.setattr(extraction, "EXTRACTION_TIMEOUT_SECONDS", 60.0)
    with pytest.raises(ExtractionFailure) as memory:
        extraction.extract(DocumentFormat.PDF, b"%PDF", "a.pdf")
    assert memory.value.reason == RESOURCE_LIMIT


def test_only_trusted_sqlite_extension_loads(
    store: HistoryStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = Database(store.database_path)
    with pytest.raises(sqlite3.OperationalError):
        plain.read(lambda conn: conn.execute("SELECT vec_version()").fetchone())
    loaded = Database(store.database_path, connector=retrieval.vec_connector)
    assert loaded.read(lambda conn: conn.execute("SELECT vec_version()").fetchone()[0])

    import sqlite_vec

    rogue = tmp_path / "rogue"
    (tmp_path / "rogue.dll").write_bytes(b"x")
    (tmp_path / "rogue.so").write_bytes(b"x")
    monkeypatch.setattr(sqlite_vec, "loadable_path", lambda: str(rogue))
    with pytest.raises(SourcesError) as caught:
        retrieval.vec_connector(":memory:")
    assert caught.value.code == "sources_unavailable"
    assert os.path.exists(rogue.with_suffix(".dll"))


def test_real_scanner_excludes_secret_document(
    store: HistoryStore, workdir: Path, gitleaks: str
) -> None:
    from lastro.sources.safety import gitleaks_scanner

    secret = 'aws_access_key_id = "' + "AKIA" + "IOSFODNN7" + "EXAMPLQ" + '"'
    docs = write_docs(
        workdir / "docs", {"ok.md": "# Ok\n\nsem segredo", "chaves.md": f"# Chaves\n\n{secret}"}
    )
    service = make_service(store)
    service._scanner_factory = gitleaks_scanner  # pyright: ignore[reportPrivateUsage]

    result = service.add(docs, "work")

    assert result.reasons == {"possible_secret": 1}
    assert "IOSFODNN7" not in _all_rows(store.database_path)
