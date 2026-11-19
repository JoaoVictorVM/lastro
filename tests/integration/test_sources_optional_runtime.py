"""Smoke com pacotes e modelo reais: opt-in, porque baixa gigabytes ou chama um provedor.

LASTRO_REAL_MODEL_ROOT aponta para um private data root já preparado com
`lastro sources model prepare`; LASTRO_REAL_RAGAS=1 habilita o Ragas com o juiz configurado.
Dublês nunca contam como evidência de qualidade.
"""

import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from lastro.sources import embeddings
from lastro.sources.embeddings import FastEmbedModel, ModelStatus

pytest.importorskip("sqlite_vec")


def test_offline_queries_do_not_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class TextEmbedding:
        def __init__(self, **kwargs: Any) -> None:
            calls.append(kwargs)

        def embed(self, texts: list[str], batch_size: int) -> list[list[float]]:
            return [[1.0] + [0.0] * 1023 for _ in texts]

    cache = embeddings.models_dir(tmp_path)
    snapshot = cache / "snap"
    snapshot.mkdir(parents=True)
    (snapshot / "tokenizer.json").write_text("{}", encoding="utf-8")
    manifest = {"directory": "snap", "files": {"tokenizer.json": {"sha256": "a" * 64, "size": 2}}}
    monkeypatch.setattr(
        embeddings, "model_status", lambda root: ModelStatus(True, None, "f" * 64, "ok")
    )
    monkeypatch.setattr(embeddings, "read_manifest", lambda directory: manifest)
    monkeypatch.setattr(embeddings, "HuggingFaceTokenizer", lambda path, fingerprint: object())
    monkeypatch.setitem(
        sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=TextEmbedding)
    )
    monkeypatch.setattr(
        embeddings, "_download", lambda cache: (_ for _ in ()).throw(AssertionError("download"))
    )

    model = FastEmbedModel(tmp_path)
    vector = model.embed_query("email")

    assert calls == [
        {"model_name": embeddings.MODEL_NAME, "cache_dir": str(cache), "local_files_only": True}
    ]
    assert len(vector) == 4096


@pytest.mark.skipif(
    not os.environ.get("LASTRO_REAL_MODEL_ROOT"), reason="modelo real não preparado (opt-in)"
)
def test_real_fastembed_sqlite_vec_smoke(
    history_context: Any, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.integration.sources_support import CleanScanner

    from lastro.history import open_history
    from lastro.sources.conflicts import declared_facts_hook
    from lastro.sources.sync import SourceService

    root = Path(os.environ["LASTRO_REAL_MODEL_ROOT"])
    pytest.importorskip("fastembed")
    history = open_history(history_context)
    service = SourceService(
        history.sources,
        project_names=("work", history.project_id),
        model_factory=lambda: FastEmbedModel(root),
        scanner_factory=CleanScanner,
        publish_hook=declared_facts_hook(history.project_id),
    )
    docs = Path(__file__).resolve().parents[2] / "evals" / "retrieval" / "demo" / "docs"
    service.add(docs, "work")

    outcome = service.query(run_id=None, flow="f", text="O email é obrigatório?", limit=5)

    assert outcome.passages
    assert outcome.passages[0].citation.startswith("[docs/")


@pytest.mark.skipif(not os.environ.get("LASTRO_REAL_RAGAS"), reason="Ragas real opt-in")
def test_real_ragas_adapter_smoke() -> None:
    pytest.importorskip("ragas")
    from lastro.evaluation.executors import default_judge
    from lastro.evaluation.ragas_adapter import RagasAdapter, RetrievalCase

    judge = default_judge(os.environ.get("LASTRO_REAL_RAGAS_JUDGE", "claude-code"), None)
    workdir = Path(os.environ.get("LASTRO_REAL_RAGAS_WORKDIR", "."))
    scores = RagasAdapter().score(
        RetrievalCase(
            "O email é obrigatório?",
            ("O email é opcional no cadastro de funcionário.",),
            "Não, o email é opcional.",
            "Não. O email é opcional.",
        ),
        lambda prompt: judge.ask(prompt, workdir)[0],
    )

    assert set(scores) == {
        "retrieval.context_precision",
        "retrieval.context_recall",
        "retrieval.faithfulness",
    }
