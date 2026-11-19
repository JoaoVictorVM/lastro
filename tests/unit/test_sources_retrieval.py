from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest
from tests.integration.sources_support import make_service, write_docs

from lastro.config.loader import ProjectContext
from lastro.history import HistoryStore, open_history
from lastro.history.sources import DocumentDraft
from lastro.sources.chunking import chunk_document, chunking_fingerprint
from lastro.sources.embeddings import HashingModel
from lastro.sources.extraction import extraction_fingerprint
from lastro.sources.models import Candidate, Extracted, Passage, Section
from lastro.sources.retrieval import fit_passages, fuse, match_expression


def _candidate(chunk: str, path: str = "a.md", ordinal: int = 0) -> Candidate:
    return Candidate(chunk, "doc", path, ordinal)


def test_rrf_rank_and_stable_ties() -> None:
    lexical = [_candidate("a"), _candidate("b"), _candidate("c")]
    vector = [_candidate("c"), _candidate("a"), _candidate("d")]

    ranked = fuse(lexical, vector, 5)

    scores = {item.chunk_id: score for item, score in ranked}
    assert scores["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert scores["c"] == pytest.approx(1 / 63 + 1 / 61)
    assert [item.chunk_id for item, _ in ranked] == ["a", "c", "b", "d"]
    tied = fuse([_candidate("z", "b.md"), _candidate("y", "a.md")], [], 5)
    assert [
        item.chunk_id for item, _ in fuse([_candidate("y", "a.md")], [_candidate("z", "b.md")], 5)
    ] == [
        "y",
        "z",
    ]
    assert [item.chunk_id for item, _ in tied] == ["z", "y"]
    assert len(fuse(lexical, vector, 2)) == 2


def test_literal_fts_terms() -> None:
    expression = match_expression('email" OR * NEAR(senha) -campo:x')
    assert expression == '"email" OR "OR" OR "NEAR" OR "senha" OR "campo" OR "x"'
    assert match_expression("  ** ") is None


def _passage(rank: int, tokens: int, text: str = "x") -> Passage:
    return Passage(
        reference_id="",
        chunk_id=f"chunk_{rank}",
        text=text,
        citation=f"[docs/a.md § H{rank}]",
        version={"file_sha256": "a" * 64, "text_sha256": "b" * 64, "modified_at": None},
        locator={"path": "docs/a.md", "heading": f"H{rank}"},
        contested=False,
        token_count=tokens,
        rank=rank,
    )


def test_response_budget_keeps_citations() -> None:
    passages = [_passage(rank, 900) for rank in range(1, 6)]

    included, omitted = fit_passages(passages)

    assert [item.rank for item in included] == [1, 2, 3, 4]
    assert omitted == 1
    assert all(item.as_json()["citation"] for item in included)
    large = [_passage(rank, 10, "y" * 15000) for rank in range(1, 6)]
    kept, dropped = fit_passages(large)
    assert len(kept) == 3 and dropped == 2


@pytest.fixture
def store(history_context: ProjectContext, fake_clock: Callable[[], datetime]) -> HistoryStore:
    pytest.importorskip("sqlite_vec")
    pytest.importorskip("markdown_it")
    return open_history(history_context, clock=fake_clock)


def test_window_hits_deduplicate_before_top_k(store: HistoryStore, workdir: Path) -> None:
    words = " ".join(["prazo"] * 1200) + " " + " ".join(f"x{index}" for index in range(10))
    docs = write_docs(workdir / "docs", {"longo.md": "# Longo\n\n" + words, "b.md": "# B\n\nprazo"})
    service = make_service(store)
    service.add(docs, "work")

    outcome = service.query(run_id=None, flow="f", text="prazo", limit=5)

    chunk_ids = [item.chunk_id for item in outcome.passages]
    assert len(chunk_ids) == len(set(chunk_ids))
    assert len(outcome.passages) == 3


def test_scope_filter_before_candidate_limit(store: HistoryStore, workdir: Path) -> None:
    docs = write_docs(workdir / "docs", {"ativo.md": "# Ativo\n\nbanana"})
    service = make_service(store)
    service.add(docs, "work")
    repository = store.sources
    model = HashingModel()
    lease = repository.acquire("staging-writer", service.fingerprint())
    source_id = repository.roots()[0].id
    chunking = chunking_fingerprint(model.tokenizer)
    # Vinte e cinco documentos fora da geração ativa, todos com o termo buscado.
    for index in range(25):
        text = f"banana banana fora {index}"
        document = Extracted(text, (Section("Fora", text, 0),))
        chunks = chunk_document(document, f"fora{index}.md", model.tokenizer)
        repository.store_document(
            lease,
            DocumentDraft(
                source_id=source_id,
                relative_path=f"fora{index}.md",
                format="markdown",
                file_sha256=f"{index:064x}",
                text_sha256=f"{index:064x}",
                byte_size=len(text),
                normalized_text=text,
                extraction_fingerprint=extraction_fingerprint(),
                declarations=[],
            ),
            chunks,
            [model.embed_passages(list(chunk.window_texts)) for chunk in chunks],
            chunking,
            model.fingerprint,
        )

    outcome = service.query(run_id=None, flow="f", text="banana", limit=5)

    assert [str(item.locator["path"]) for item in outcome.passages] == ["docs/ativo.md"]
    repository.release(lease)
