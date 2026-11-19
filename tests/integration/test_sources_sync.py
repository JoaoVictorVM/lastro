import json
import os
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from tests.integration.sources_support import (
    CADASTRO,
    CleanScanner,
    make_pdf,
    make_service,
    write_docs,
)
from typer.testing import CliRunner

from lastro.cli import sources_cmd
from lastro.cli.app import app
from lastro.config.loader import ProjectContext
from lastro.history import HistoryStore, open_history
from lastro.history.sources import ActiveFile, FileRow
from lastro.sources import safety
from lastro.sources.embeddings import HashingModel
from lastro.sources.models import SourcesError
from lastro.sources.safety import check_limits

pytest.importorskip("sqlite_vec")
pytest.importorskip("markdown_it")
pytest.importorskip("bs4")
pytest.importorskip("pypdf")


@pytest.fixture
def store(history_context: ProjectContext, fake_clock: Callable[[], datetime]) -> HistoryStore:
    return open_history(history_context, clock=fake_clock)


@pytest.fixture
def docs(workdir: Path) -> Path:
    return write_docs(
        workdir / "docs",
        {
            "cadastro.md": CADASTRO,
            "notas.txt": "Relatórios mensais ficam disponíveis no primeiro dia útil.",
            "ajuda.html": "<h1>Ajuda</h1><p>Use o botão Salvar para gravar o funcionário.</p>",
            "manual.pdf": make_pdf(["Exportacao de folha gera arquivo CSV."]),
        },
    )


def _paths(store: HistoryStore, query: str) -> list[str]:
    service = make_service(store)
    outcome = service.query(run_id=None, flow="employee.create", text=query, limit=5)
    return [str(passage.locator["path"]) for passage in outcome.passages]


def test_add_and_sync_incremental(store: HistoryStore, docs: Path) -> None:
    model = HashingModel()
    service = make_service(store, model=model)

    first = service.add(docs, "work")

    assert first.status == "ok"
    assert first.files.as_json() == {
        "added": 4,
        "updated": 0,
        "removed": 0,
        "unchanged": 0,
        "excluded": 0,
        "failed": 0,
    }
    assert first.active_chunks >= 4
    outcome = service.query(run_id=None, flow="employee.create", text="email opcional", limit=5)
    top = outcome.passages[0]
    assert top.citation == "[docs/cadastro.md § Campos obrigatórios]"
    assert top.locator["line_start"] == 5
    assert len(str(top.version["file_sha256"])) == 64
    assert outcome.as_json()["mode"] == "hybrid"

    (docs / "notas.txt").write_text("Relatórios agora saem no quinto dia útil.", encoding="utf-8")
    (docs / "novo.md").write_text("# Novo\n\nFérias exigem aprovação do gestor.", encoding="utf-8")
    calls = model.calls
    second = service.sync()

    assert (second.files.added, second.files.updated, second.files.unchanged) == (1, 1, 3)
    assert second.generation == first.generation + 1
    assert model.calls > calls
    again = service.add(docs, "work")
    assert again.files.unchanged == 5
    assert len(store.sources.roots()) == 1


def test_hash_detects_same_mtime_change(store: HistoryStore, docs: Path) -> None:
    model = HashingModel()
    service = make_service(store, model=model)
    service.add(docs, "work")
    target = docs / "notas.txt"
    stat = target.stat()
    target.write_text("Relatórios saem semanalmente agora.", encoding="utf-8")
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    changed = service.sync()

    assert changed.files.updated == 1
    calls = model.calls
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    touched = service.sync()
    assert touched.files.unchanged == 4 and touched.files.updated == 0
    assert model.calls == calls


def test_deleted_file_never_retrieved(store: HistoryStore, docs: Path) -> None:
    service = make_service(store)
    service.add(docs, "work")
    assert "docs/manual.pdf" in _paths(store, "Exportacao folha CSV")

    (docs / "manual.pdf").unlink()
    result = service.sync()

    assert result.files.removed == 1
    assert "docs/manual.pdf" not in _paths(store, "Exportacao folha CSV")
    # Busca só vetorial (sem termo lexical em comum) também não devolve o documento.
    repository = store.sources
    model = HashingModel()
    vector = model.embed_query("Exportacao folha CSV")
    generation = repository.active()
    assert generation is not None
    outcome = repository.query(
        run_id=None, flow="f", query="", vector=vector, model=model.fingerprint, limit=5
    )
    assert all("manual.pdf" not in str(item.locator["path"]) for item in outcome.passages)


def test_root_unavailable_is_not_deletion(store: HistoryStore, docs: Path) -> None:
    service = make_service(store)
    service.add(docs, "work")
    before = store.sources.active()
    docs.rename(docs.parent / "moved")

    with pytest.raises(SourcesError) as caught:
        service.sync()

    assert caught.value.code == "source_unavailable"
    assert store.sources.active() == before
    assert "docs/cadastro.md" in _paths(store, "email opcional")


def test_failed_update_retires_old_chunks(store: HistoryStore, docs: Path) -> None:
    service = make_service(store)
    service.add(docs, "work")
    (docs / "notas.txt").write_bytes("Relatórios mensais".encode("latin-1"))

    result = service.sync()

    assert result.status == "partial"
    assert result.files.failed == 1
    assert result.reasons == {"unsupported_encoding": 1}
    assert "docs/notas.txt" not in _paths(store, "Relatórios mensais")


def test_2000_files_and_200mb_boundary(
    store: HistoryStore, docs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert check_limits(2000, 200_000_000) is None
    assert check_limits(2001, 1) is not None
    assert check_limits(1, 200_000_001) is not None
    service = make_service(store)
    service.add(docs, "work")
    before = store.sources.active()
    write_docs(docs, {"extra.md": "# Extra\n\nmais"})
    monkeypatch.setattr(safety, "MAX_FILES", 4)

    with pytest.raises(SourcesError) as caught:
        service.sync()

    assert caught.value.code == "corpus_limit_exceeded"
    assert store.sources.active() == before


def test_concurrent_sync_fencing(
    store: HistoryStore, docs: Path, clock_now: list[datetime]
) -> None:
    service = make_service(store)
    service.add(docs, "work")
    repository = store.sources
    first = repository.acquire("writer-a", service.fingerprint())

    with pytest.raises(SourcesError) as busy:
        service.sync()
    assert busy.value.code == "sources_busy"

    clock_now[0] += timedelta(seconds=121)
    second = repository.acquire("writer-b", service.fingerprint())
    with pytest.raises(SourcesError) as fenced:
        repository.publish(first, [], [])
    assert fenced.value.code == "catalog_revision_conflict"
    repository.release(second)
    repository.release(first)
    assert service.sync().status == "ok"


def test_crash_before_and_after_publish(
    store: HistoryStore, docs: Path, clock_now: list[datetime]
) -> None:
    service = make_service(store)
    first = service.add(docs, "work")
    repository = store.sources
    # Escritor que morre antes de publicar: staging fica órfão e o lease vence.
    repository.acquire("dead-writer", service.fingerprint())
    assert repository.active() is not None and repository.active_chunks() == first.active_chunks

    clock_now[0] += timedelta(seconds=121)
    after_crash = service.sync()
    assert after_crash.generation == first.generation + 1
    assert after_crash.files.unchanged == 4

    # Escritor que publica e morre antes de liberar: nada é publicado duas vezes.
    lease = repository.acquire("publish-then-die", service.fingerprint())
    files = _rows(repository.active_files())
    ordinal = repository.publish(lease, files, [])
    clock_now[0] += timedelta(seconds=121)
    final = service.sync()
    assert final.generation == ordinal + 1
    assert final.files.unchanged == 4


def _rows(active: dict[tuple[str, str], ActiveFile]) -> list[FileRow]:
    rows: list[FileRow] = []
    for item in active.values():
        rows.append(
            FileRow(
                item.source_id,
                item.relative_path,
                item.status,
                item.document_id,
                item.modified_at,
                item.reason,
            )
        )
    return rows


def test_cli_add_list_and_sync(
    history_context: ProjectContext,
    docs: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sources_cmd, "model_factory", lambda root: HashingModel())
    monkeypatch.setattr(sources_cmd, "scanner_factory", CleanScanner)

    added = runner.invoke(app, ["sources", "add", str(docs), "--project", "work"])
    assert added.exit_code == 0, added.output
    assert "4 adicionados" in added.output
    assert "Chunks ativos" in added.output

    listed = runner.invoke(app, ["sources", "list", "--json"])
    assert listed.exit_code == 0
    data = json.loads(listed.output)
    assert data["sources"][0]["label"] == "docs"
    assert str(docs) not in listed.output and docs.as_posix() not in listed.output

    (docs / "notas.txt").unlink()
    synced = runner.invoke(app, ["sources", "sync", "--json"])
    assert synced.exit_code == 0, synced.output
    assert json.loads(synced.output)["files"]["removed"] == 1

    wrong = runner.invoke(app, ["sources", "add", str(docs), "--project", "outro", "--json"])
    assert wrong.exit_code == 2
    assert json.loads(wrong.output)["code"] == "invalid_sources_input"

    (docs / "ruim.txt").write_bytes(b"\xff\xfe\x00")
    partial = runner.invoke(app, ["sources", "sync"])
    assert partial.exit_code == 1
    assert "parcialmente" in partial.output
