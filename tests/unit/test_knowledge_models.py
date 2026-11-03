from typing import Any

import pytest
from pydantic import ValidationError

from lastro.knowledge import ProposalInput, Scope
from lastro.knowledge.facts import identity, render_statement
from lastro.knowledge.models import (
    ExperienceFact,
    FactDocument,
    FieldPropertyFact,
    ScreenFact,
)


def proposal(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "flow": "employee",
        "category": "screen_component",
        "fact": {
            "type": "field_property",
            "subject": "employee",
            "field": "cpf",
            "attribute": "required",
            "value": True,
        },
        "observation_ids": ["obs_1"],
        "idempotency_key": "k1",
    }
    data.update(overrides)
    return data


def test_fact_union_rejects_extra_fields() -> None:
    fact = proposal()["fact"] | {"statement": "free text from the page"}

    with pytest.raises(ValidationError):
        ProposalInput.model_validate(proposal(fact=fact))
    with pytest.raises(ValidationError):
        ProposalInput.model_validate(proposal(extra="x"))
    with pytest.raises(ValidationError):
        FactDocument.model_validate({"fact": {"type": "unknown"}})


def test_fact_values_are_typed_by_attribute() -> None:
    fact = proposal()["fact"]
    with pytest.raises(ValidationError):
        ProposalInput.model_validate(proposal(fact=fact | {"value": "cpf"}))
    with pytest.raises(ValidationError):
        ProposalInput.model_validate(proposal(fact=fact | {"attribute": "format", "value": True}))
    with pytest.raises(ValidationError):
        ProposalInput.model_validate(proposal(fact=fact | {"field": "CPF Field"}))
    parsed = ProposalInput.model_validate(
        proposal(fact=fact | {"attribute": "format", "value": "cpf"})
    )
    assert isinstance(parsed.fact, FieldPropertyFact) and parsed.fact.value == "cpf"


def test_page_quotes_are_under_200_chars() -> None:
    landmark = {"role": "heading", "label": "x" * 199}
    screen = {"type": "screen", "route": "/employees", "landmarks": [landmark]}
    ProposalInput.model_validate(proposal(fact=screen))

    too_long = {**screen, "landmarks": [{"role": "heading", "label": "x" * 200}]}
    with pytest.raises(ValidationError):
        ProposalInput.model_validate(proposal(fact=too_long))
    with pytest.raises(ValidationError):
        ProposalInput.model_validate(proposal(fact={**screen, "landmarks": [landmark] * 21}))
    with pytest.raises(ValidationError):
        ProposalInput.model_validate(proposal(fact={**screen, "route": "/employees?x=1"}))


def test_statement_is_rendered_from_fact() -> None:
    fact = FieldPropertyFact(
        type="field_property", subject="employee", field="cpf", attribute="required", value=True
    )
    screen = ScreenFact.model_validate(
        {
            "type": "screen",
            "route": "/employees",
            "landmarks": [{"role": "heading", "label": "Funcionários"}],
        }
    )

    assert render_statement(fact) == "employee.cpf: required=true"
    assert render_statement(screen) == 'screen /employees has heading "Funcionários"'


def test_category_must_match_fact_type() -> None:
    with pytest.raises(ValidationError):
        ProposalInput.model_validate(proposal(category="flow"))
    with pytest.raises(ValidationError):
        ProposalInput.model_validate(proposal(observation_ids=[]))
    clarification = {"type": "clarification", "gap_id": "g1", "subject": "employee"}
    with pytest.raises(ValidationError):
        ProposalInput.model_validate(
            proposal(category="rule_clarification", fact=clarification, observation_ids=[])
        )
    with pytest.raises(ValidationError):
        ProposalInput.model_validate(
            proposal(
                category="rule_clarification",
                fact={**clarification, "answer": "the agent answered itself"},
                clarification_id="c1",
            )
        )


def test_account_permission_cannot_be_generalized() -> None:
    blocked = ExperienceFact.model_validate(
        {
            "type": "experience",
            "attempt": "update",
            "outcome": "blocked",
            "context": {"route": "/employees/1", "observation_id": "obs_1"},
        }
    )
    global_scope = Scope(None, None, None, None, None)
    run_scope = Scope("http://h:80/", "demo", "aurora", None, "run_1")
    other_run = Scope("http://h:80/", "demo", "aurora", None, "run_2")

    assert blocked.is_permission
    assert not run_scope.covers(other_run)
    assert global_scope.covers(run_scope)
    # Tentativas diferentes têm chaves diferentes: uma falha nunca contradiz outra.
    other = blocked.model_copy(
        update={"context": blocked.context.model_copy(update={"observation_id": "obs_2"})}
    )
    assert identity(blocked).key != identity(other).key
