from pathlib import Path
from typing import Any

import pytest
from tests.integration.sources_support import CADASTRO, make_service, write_docs

from lastro.history import HistoryStore, RunStatus
from lastro.knowledge import ProposalInput, ValidationState
from lastro.sources.conflicts import report_conflict
from lastro.sources.models import ConflictReportInput, SourcesError

pytest.importorskip("sqlite_vec")
pytest.importorskip("markdown_it")

FLOW = "employee.create"


def email_required(observation: str, value: bool = True, key: str = "email") -> ProposalInput:
    return ProposalInput.model_validate(
        {
            "flow": FLOW,
            "category": "screen_component",
            "fact": {
                "type": "field_property",
                "subject": "employee",
                "field": "email",
                "attribute": "required",
                "value": value,
            },
            "observation_ids": [observation],
            "idempotency_key": key,
        }
    )


def documented(history: HistoryStore, workdir: Path, text: str = CADASTRO) -> Path:
    docs = write_docs(workdir / "docs", {"cadastro.md": text})
    make_service(history).add(docs, "work")
    return docs


def observe_required(knowledge: Any, value: bool = True) -> tuple[HistoryStore, str, str]:
    history, run_id = knowledge.start()
    observation = knowledge.observe(
        history,
        run_id,
        fields=[{"name": "email", "label": "Email", "required": value, "visible": True}],
    )
    history.propose_knowledge(run_id, email_required(observation, value))
    return history, run_id, observation


def doc_entry(history: HistoryStore) -> Any:
    (entry,) = [
        item
        for item in history.list_knowledge(flow=FLOW)
        if item.source_type.value == "documentation"
    ]
    return entry


def test_f15_document_provenance_and_contested_entry(knowledge: Any, workdir: Path) -> None:
    history = knowledge.history()
    documented(history, workdir)
    declared = doc_entry(history)
    assert declared.statement == "employee.email: required=false"
    assert declared.validation_state == ValidationState.OBSERVED
    assert history.knowledge_extensions(declared.id)["adapter"] == "documentation"

    history, run_id, _ = observe_required(knowledge)
    history.finish_run(run_id, RunStatus.COMPLETED)

    entries = {item.source_type.value: item for item in history.list_knowledge(flow=FLOW)}
    assert entries["observation"].validation_state == ValidationState.CONTESTED
    assert entries["documentation"].validation_state == ValidationState.CONTESTED
    assert entries["observation"].scope.tenant == "acme"
    assert entries["documentation"].scope.tenant is None
    (conflict,) = history.sources.conflicts(run_id)
    assert conflict.assessment.value == "structured"
    assert {str(conflict.left["source_type"]), str(conflict.right["source_type"])} == {
        "documentation",
        "observation",
    }
    assert set(conflict.knowledge_ids) == {entries["observation"].id, declared.id}


def test_external_dispute_survives_memory_consolidation(knowledge: Any, workdir: Path) -> None:
    history, run_id = knowledge.start()
    documented(history, workdir, "# Cadastro\n\n## Regras\n\nCPF pode ficar em branco.")
    observation = knowledge.observe(history, run_id)
    knowledge.propose(history, run_id, observation, value=True)
    history.finish_run(run_id, RunStatus.COMPLETED)
    (entry,) = [
        item for item in history.list_knowledge() if item.source_type.value == "observation"
    ]

    history, second = knowledge.start()
    from lastro.knowledge import QueryInput

    history.query_knowledge(second, QueryInput(flow="employee", query="cpf"))
    passage = make_service(history).query(
        run_id=second, flow="employee", text="CPF branco", limit=1
    )
    receipt = report_conflict(
        history,
        second,
        ConflictReportInput.model_validate(
            {
                "flow": "employee",
                "left": {
                    "kind": "passage",
                    "reference_id": passage.passages[0].reference_id,
                    "quote": "CPF pode ficar em branco",
                },
                "right": {
                    "kind": "knowledge",
                    "reference_id": entry.id,
                    "quote": "employee.cpf: required=true",
                },
                "explanation": "A documentação aceita CPF vazio; a memória diz obrigatório.",
                "idempotency_key": "cpf",
            }
        ),
    )
    assert receipt.contested_knowledge_ids == (entry.id,)
    assert history.knowledge_detail(entry.id).entry.validation_state == ValidationState.CONTESTED

    # Uma nova evidência do mesmo valor não devolve o estado observed enquanto a disputa existe.
    observation = knowledge.observe(history, second)
    knowledge.propose(history, second, observation, value=True, key="again")
    history.finish_run(second, RunStatus.COMPLETED)
    assert history.knowledge_detail(entry.id).entry.validation_state == ValidationState.CONTESTED


def test_sync_does_not_resolve_conflict(knowledge: Any, workdir: Path) -> None:
    history = knowledge.history()
    docs = documented(history, workdir)
    history, run_id, _ = observe_required(knowledge)
    history.finish_run(run_id, RunStatus.COMPLETED)
    (conflict,) = history.sources.conflicts(run_id)

    (docs / "cadastro.md").unlink()
    write_docs(docs, {"outro.md": "# Outro\n\nnada relevante"})
    make_service(history).sync()

    observed = next(
        item
        for item in history.list_knowledge(flow=FLOW)
        if item.source_type.value == "observation"
    )
    assert observed.validation_state == ValidationState.CONTESTED
    assert [item.status for item in history.sources.conflicts(run_id)] == ["open"]
    assert history.sources.conflicts(run_id)[0].id == conflict.id
    # A entrada só documental saiu do uso automático, sem apagar o histórico.
    assert all(
        item.source_type.value != "documentation" for item in history.list_knowledge(flow=FLOW)
    )
    detail = history.knowledge_detail(conflict.knowledge_ids[0])
    assert detail.history


def test_source_withdrawal_preserves_other_evidence(knowledge: Any, workdir: Path) -> None:
    from lastro.history.knowledge import _add_evidence  # pyright: ignore[reportPrivateUsage]

    history = knowledge.history()
    docs = documented(history, workdir)
    declared = doc_entry(history)
    # Evidência observada do mesmo valor anexada à entrada documental.
    other, run = knowledge.start()
    observation = knowledge.observe(other, run)
    other._database.write(  # pyright: ignore[reportPrivateUsage]
        lambda conn: _add_evidence(
            conn, declared.id, run, "2026-11-19T00:00:00.000000Z", observation_id=observation
        )
    )

    (docs / "cadastro.md").unlink()
    write_docs(docs, {"outro.md": "# Outro\n\nnada"})
    make_service(history).sync()

    kept = history.knowledge_detail(declared.id)
    assert kept.entry.discarded_at is None
    sources = {item["type"] for item in kept_sources(history, declared.id)}
    assert {"documentation"} <= sources


def kept_sources(history: HistoryStore, entry_id: str) -> list[dict[str, Any]]:
    from lastro.history.knowledge import entry_sources

    return history._database.read(  # pyright: ignore[reportPrivateUsage]
        lambda conn: [
            dict(item) for item in entry_sources(conn, entry_id) if isinstance(item, dict)
        ]
    )


def test_conflict_request_idempotency(knowledge: Any, workdir: Path) -> None:
    history, run_id = knowledge.start()
    documented(history, workdir)
    observation = knowledge.observe(
        history,
        run_id,
        fields=[{"name": "email", "label": "Email obrigatório", "required": True}],
    )
    passage = make_service(history).query(run_id=run_id, flow=FLOW, text="email opcional", limit=1)
    body: dict[str, Any] = {
        "flow": FLOW,
        "left": {
            "kind": "passage",
            "reference_id": passage.passages[0].reference_id,
            "quote": "O email é opcional",
        },
        "right": {"kind": "observation", "reference_id": observation, "quote": "Email obrigatório"},
        "explanation": "A documentação permite omitir o email; a interface o exige.",
        "idempotency_key": "email",
    }

    first = report_conflict(history, run_id, ConflictReportInput.model_validate(body))
    again = report_conflict(history, run_id, ConflictReportInput.model_validate(body))

    assert first.conflict_id == again.conflict_id and again.deduplicated
    assert first.assessment.value == "reported" and first.status == "open"
    changed = {**body, "explanation": "Outra explicação."}
    with pytest.raises(SourcesError) as caught:
        report_conflict(history, run_id, ConflictReportInput.model_validate(changed))
    assert caught.value.code == "idempotency_conflict"
