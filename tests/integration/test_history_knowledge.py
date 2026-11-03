import json
import sqlite3
from typing import Any

import pytest

from lastro.history import EventType, KnowledgeError, RunStatus
from lastro.knowledge import ProposalInput, QueryInput, SourceType, ValidationState


def experience(observation_id: str, route: str = "/employees/new") -> ProposalInput:
    return ProposalInput.model_validate(
        {
            "flow": "employee",
            "category": "experience",
            "fact": {
                "type": "experience",
                "attempt": "update",
                "outcome": "blocked",
                "context": {"route": route, "observation_id": observation_id},
                "message": "Sem permissão",
            },
            "observation_ids": [observation_id],
            "idempotency_key": "blocked-" + observation_id,
        }
    )


def test_proposal_and_event_are_atomic(knowledge: Any) -> None:
    history, run_id = knowledge.start()
    observation = knowledge.observe(history, run_id)

    receipt = knowledge.propose(history, run_id, observation)

    events = [
        e for e in history.list_events(run_id) if e.event_type == EventType.KNOWLEDGE_PROPOSED
    ]
    assert receipt.status == "pending" and receipt.source == SourceType.OBSERVATION
    assert receipt.validation_state == ValidationState.OBSERVED
    assert [e.payload["proposal_id"] for e in events] == [receipt.proposal_id]
    assert events[0].payload["subtype"] == "accepted"
    # Pendente não é memória consolidada: outra consulta ainda não vê a proposta.
    assert history.list_knowledge() == []


def test_proposal_rolls_back_when_event_fails(knowledge: Any) -> None:
    history, run_id = knowledge.start()
    observation = knowledge.observe(history, run_id)
    conn = sqlite3.connect(history.database_path)
    try:
        conn.execute(
            "CREATE TRIGGER fail_event BEFORE INSERT ON events "
            "WHEN NEW.event_type = 'knowledge_proposed' "
            "BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(sqlite3.IntegrityError):
        knowledge.propose(history, run_id, observation)

    conn = sqlite3.connect(history.database_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM knowledge_pending").fetchone()[0] == 0
    finally:
        conn.close()


def test_proposal_idempotency_conflict(knowledge: Any) -> None:
    history, run_id = knowledge.start()
    observation = knowledge.observe(history, run_id)

    first = knowledge.propose(history, run_id, observation, key="same")
    again = knowledge.propose(history, run_id, observation, key="same")

    assert again.proposal_id == first.proposal_id
    with pytest.raises(KnowledgeError) as info:
        knowledge.propose(history, run_id, observation, key="same", value=False)
    assert info.value.code == "revision_conflict"


def test_evidence_belongs_to_source_run(knowledge: Any) -> None:
    history, first = knowledge.start()
    old_observation = knowledge.observe(history, first)
    history.finish_run(first, RunStatus.COMPLETED)
    history, second = knowledge.start()

    with pytest.raises(KnowledgeError) as info:
        knowledge.propose(history, second, old_observation)
    assert info.value.code == "invalid_reference"

    observation = knowledge.observe(history, second, route="/employees/new")
    forged = ProposalInput.model_validate(
        {
            "flow": "employee",
            "category": "screen_component",
            "fact": {"type": "screen", "route": "/admin", "landmarks": []},
            "observation_ids": [observation],
            "idempotency_key": "forged-route",
        }
    )
    with pytest.raises(KnowledgeError) as info:
        history.propose_knowledge(second, forged)
    assert info.value.code == "invalid_reference"


def test_permission_without_account_is_run_local(knowledge: Any) -> None:
    history, first = knowledge.start()
    observation = knowledge.observe(history, first)
    history.propose_knowledge(first, experience(observation))
    history.finish_run(first, RunStatus.COMPLETED)

    (entry,) = history.list_knowledge()
    assert entry.scope.origin_run_only == first and entry.scope.account is None
    history, second = knowledge.start()
    result = history.query_knowledge(second, QueryInput(flow="employee"))
    assert result.returned_count == 0


def test_scope_filters_prevent_cross_tenant_reads(knowledge: Any) -> None:
    history, run_id = knowledge.start("homolog")
    knowledge.propose(history, run_id, knowledge.observe(history, run_id))
    history.finish_run(run_id, RunStatus.COMPLETED)

    other, other_run = knowledge.start("local")
    assert other.query_knowledge(other_run, QueryInput(flow="employee")).returned_count == 0
    other.finish_run(other_run, RunStatus.COMPLETED)

    same, same_run = knowledge.start("homolog")
    assert same.query_knowledge(same_run, QueryInput(flow="employee")).returned_count == 1
    assert same.query_knowledge(same_run, QueryInput(flow="other")).returned_count == 0


def test_bm25_order_is_stable(knowledge: Any) -> None:
    history, run_id = knowledge.start()
    observation = knowledge.observe(history, run_id)
    knowledge.propose(history, run_id, observation, attribute="required", value=True)
    knowledge.propose(history, run_id, observation, attribute="visible", value=True)
    knowledge.propose(history, run_id, observation, attribute="format", value="cpf")
    history.finish_run(run_id, RunStatus.COMPLETED)

    history, second = knowledge.start()
    first_result = history.query_knowledge(second, QueryInput(flow="employee", query="cpf format"))
    again = history.query_knowledge(second, QueryInput(flow="employee", query="cpf format"))
    content = first_result.content.split("\n", 1)[1].rsplit("\n", 1)[0]
    entries = json.loads(content)["entries"]

    assert first_result.content == again.content
    assert entries[0]["statement"] == "employee.cpf: format=cpf"
    assert len(entries) == 3


def test_query_requires_active_run(knowledge: Any) -> None:
    history, run_id = knowledge.start()
    history.finish_run(run_id, RunStatus.COMPLETED)

    with pytest.raises(KnowledgeError) as info:
        history.query_knowledge(run_id, QueryInput(flow="employee"))
    assert info.value.code == "no_active_run"


def test_trusted_adapters_extend_sources_and_contest(knowledge: Any) -> None:
    from lastro.knowledge import Category
    from lastro.knowledge.models import FieldPropertyFact

    history, run_id = knowledge.start()
    knowledge.propose(history, run_id, knowledge.observe(history, run_id), value=True)
    history.finish_run(run_id, RunStatus.COMPLETED)
    (observed,) = history.list_knowledge()

    def fact(value: bool) -> FieldPropertyFact:
        return FieldPropertyFact(
            type="field_property",
            subject="employee",
            field="cpf",
            attribute="required",
            value=value,
        )

    same = history.record_adapter_knowledge(
        adapter="fake-docs",
        source=SourceType.DOCUMENTATION,
        source_ref="docs/employees.md#cpf",
        flow="employee",
        category=Category.SCREEN_COMPONENT,
        fact=fact(True),
        scope=observed.scope,
    )
    inferred = history.record_adapter_knowledge(
        adapter="fake-code",
        source=SourceType.CODE,
        source_ref="src/employees/form.py:42",
        flow="employee",
        category=Category.SCREEN_COMPONENT,
        fact=fact(False),
        scope=observed.scope,
    )

    assert same.id == observed.id
    assert history.knowledge_detail(observed.id).history[-1].actor == "adapter"
    assert inferred.source_type == SourceType.CODE
    assert inferred.validation_state == ValidationState.CONTESTED
    assert history.knowledge_extensions(inferred.id)["inference"] == "implementation"
    assert history.knowledge_detail(observed.id).entry.conflict_ids == (inferred.id,)
    with pytest.raises(KnowledgeError):
        history.record_adapter_knowledge(
            adapter="fake",
            source=SourceType.OBSERVATION,
            source_ref="x",
            flow="employee",
            category=Category.SCREEN_COMPONENT,
            fact=fact(True),
            scope=observed.scope,
        )
