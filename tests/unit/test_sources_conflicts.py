import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from tests.integration.sources_support import make_service, write_docs

from lastro.config.loader import ProjectContext
from lastro.history import EventType, HistoryStore, RunStatus, TextMission, open_history
from lastro.history.knowledge import entry_sources
from lastro.knowledge import ProposalInput, QueryInput, SourceType, ValidationState
from lastro.sources.conflicts import report_conflict, validate_declarations
from lastro.sources.models import ConflictReportInput, Declaration, SourcesError

FLOW = "employee.create"


def fact_block(value: bool, flow: str = FLOW) -> str:
    return (
        "```lastro-fact\n"
        f'{{"flow": "{flow}", "category": "screen_component", "fact": {{"type": '
        '"field_property", "subject": "employee", "field": "email", "attribute": "required", '
        f'"value": {"true" if value else "false"}}}}}\n'
        "```\n"
    )


def doc(value: bool, title: str = "Cadastro") -> str:
    return f"# {title}\n\n## Campos\n\nO email é opcional.\n\n{fact_block(value)}"


@pytest.fixture
def histories(
    history_context_for: Callable[[str], ProjectContext],
) -> Callable[[str], HistoryStore]:
    pytest.importorskip("sqlite_vec")
    pytest.importorskip("markdown_it")
    return lambda profile: open_history(history_context_for(profile))


def run_with_email(history: HistoryStore, context: ProjectContext, value: bool) -> tuple[str, str]:
    run_id = history.create_run(context, TextMission("Explore")).record.id
    action = uuid.uuid4().hex
    history.append_event(run_id, EventType.TOOL_CALL, tool_name="observe", correlation_id=action)
    observation = history.record_observation(
        run_id,
        action,
        {
            "route": "/employees/new",
            "title": "Novo",
            "landmarks": [],
            "fields": [{"name": "email", "label": "Email obrigatório", "required": value}],
        },
    ).id
    history.propose_knowledge(
        run_id,
        ProposalInput.model_validate(
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
                "idempotency_key": f"email-{value}",
            }
        ),
    )
    return run_id, observation


def test_author_fact_preserves_documentation_origin(
    histories: Callable[[str], HistoryStore], workdir: Path
) -> None:
    history = histories("homolog")
    make_service(history).add(write_docs(workdir / "docs", {"c.md": doc(False)}), "work")

    (entry,) = history.list_knowledge(flow=FLOW)

    assert entry.source_type == SourceType.DOCUMENTATION
    assert entry.source_ref.startswith("chunk_")
    assert entry.validation_state != ValidationState.USER_PROVIDED
    assert entry.last_verified_at is None and entry.run_id is None
    detail = history.knowledge_detail(entry.id)
    assert not detail.evidence
    assert detail.history[0].actor == "adapter"


def test_invalid_metadata_does_not_create_fact(
    histories: Callable[[str], HistoryStore], workdir: Path
) -> None:
    valid, invalid = validate_declarations(
        (
            Declaration("H", '{"flow": "x", "category": "flow", "fact": {"type": "screen"}}', 1),
            Declaration("H", "not json", 2),
            Declaration("H", "x" * 5000, 3),
        )
    )
    assert valid == [] and invalid == 3

    text = '# Cadastro\n\nTexto útil sobre cadastro.\n\n```lastro-fact\n{"flow": "a"}\n```\n'
    history = histories("homolog")
    result = make_service(history).add(write_docs(workdir / "docs", {"c.md": text}), "work")

    assert result.reasons == {"fact_metadata_invalid": 1}
    assert history.list_knowledge() == []
    found = make_service(history).query(run_id=None, flow=FLOW, text="cadastro útil", limit=5)
    assert found.passages


def test_document_scope_compares_with_specific_memory(
    histories: Callable[[str], HistoryStore],
    history_context_for: Callable[[str], ProjectContext],
    workdir: Path,
) -> None:
    history = histories("homolog")
    run_id, _ = run_with_email(history, history_context_for("homolog"), True)
    history.finish_run(run_id, RunStatus.COMPLETED)

    make_service(history).add(write_docs(workdir / "docs", {"c.md": doc(False)}), "work")

    states = {item.source_type.value: item.validation_state for item in history.list_knowledge()}
    assert states == {"observation": ValidationState.CONTESTED, "documentation": "contested"}


def test_other_tenant_not_contested(
    histories: Callable[[str], HistoryStore],
    history_context_for: Callable[[str], ProjectContext],
    workdir: Path,
) -> None:
    history = histories("local")
    other_run, _ = run_with_email(history, history_context_for("local"), True)
    history.finish_run(other_run, RunStatus.COMPLETED)
    (foreign,) = history.list_knowledge(flow=FLOW)
    history = histories("homolog")
    make_service(history).add(
        write_docs(workdir / "docs", {"c.md": "# C\n\nO email é opcional."}), "work"
    )
    run_id = history.create_run(history_context_for("homolog"), TextMission("x")).record.id
    passage = make_service(history).query(run_id=run_id, flow=FLOW, text="email", limit=1)
    # A consulta da run não devolve a entrada de outro perfil; forçar a referência é recusado.
    assert history.query_knowledge(run_id, QueryInput(flow=FLOW)).returned_count == 0
    history._database.write(  # pyright: ignore[reportPrivateUsage]
        lambda conn: conn.execute(
            "INSERT INTO knowledge_run_refs (run_id, knowledge_id, revision, snapshot_json, "
            "accessed_at) VALUES (?, ?, 1, '{}', '2026-11-19T00:00:00.000000Z')",
            (run_id, foreign.id),
        )
    )

    with pytest.raises(SourcesError) as caught:
        report_conflict(
            history,
            run_id,
            ConflictReportInput.model_validate(
                {
                    "flow": FLOW,
                    "left": {
                        "kind": "passage",
                        "reference_id": passage.passages[0].reference_id,
                        "quote": "opcional",
                    },
                    "right": {
                        "kind": "knowledge",
                        "reference_id": foreign.id,
                        "quote": "required=true",
                    },
                    "explanation": "divergência",
                    "idempotency_key": "k",
                }
            ),
        )

    assert caught.value.code == "scope_mismatch"
    assert history.knowledge_detail(foreign.id).entry.validation_state == "observed"


def test_reported_conflict_is_not_verified_fact(
    histories: Callable[[str], HistoryStore],
    history_context_for: Callable[[str], ProjectContext],
    workdir: Path,
) -> None:
    history = histories("homolog")
    make_service(history).add(
        write_docs(workdir / "docs", {"c.md": "# C\n\n## Campos\n\nO email é opcional."}), "work"
    )
    run_id, observation = run_with_email(history, history_context_for("homolog"), True)
    passage = make_service(history).query(run_id=run_id, flow=FLOW, text="email", limit=1)
    before = len(history.list_knowledge(include_discarded=True))

    receipt = report_conflict(
        history,
        run_id,
        ConflictReportInput.model_validate(
            {
                "flow": FLOW,
                "left": {
                    "kind": "passage",
                    "reference_id": passage.passages[0].reference_id,
                    "quote": "O email é opcional",
                },
                "right": {
                    "kind": "observation",
                    "reference_id": observation,
                    "quote": "Email obrigatório",
                },
                "explanation": "Documentação diz opcional; tela diz obrigatório.",
                "idempotency_key": "email",
            }
        ),
    )

    assert receipt.assessment.value == "reported" and receipt.contested_knowledge_ids == ()
    assert len(history.list_knowledge(include_discarded=True)) == before
    with pytest.raises(SourcesError) as wrong_quote:
        report_conflict(
            history,
            run_id,
            ConflictReportInput.model_validate(
                {
                    "flow": FLOW,
                    "left": {
                        "kind": "passage",
                        "reference_id": passage.passages[0].reference_id,
                        "quote": "O email é proibido",
                    },
                    "right": {
                        "kind": "observation",
                        "reference_id": observation,
                        "quote": "Email obrigatório",
                    },
                    "explanation": "x",
                    "idempotency_key": "other",
                }
            ),
        )
    assert wrong_quote.value.code == "invalid_source_reference"


def test_same_value_exposes_all_sources(
    histories: Callable[[str], HistoryStore], workdir: Path
) -> None:
    history = histories("homolog")
    docs = write_docs(workdir / "docs", {"a.md": doc(False, "A"), "b.md": doc(False, "B")})
    make_service(history).add(docs, "work")

    (entry,) = history.list_knowledge(flow=FLOW)
    sources: list[Any] = history._database.read(  # pyright: ignore[reportPrivateUsage]
        lambda conn: entry_sources(conn, entry.id)
    )

    citations = sorted(str(item["citation"]) for item in sources if item.get("citation"))
    assert citations == ["[docs/a.md § Campos]", "[docs/b.md § Campos]"]
    assert all(item["type"] == "documentation" for item in sources)
