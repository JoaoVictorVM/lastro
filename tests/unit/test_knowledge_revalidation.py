from typing import Any

from lastro.knowledge.models import ExperienceFact, FieldPropertyFact, ScreenFact
from lastro.knowledge.revalidation import check


def projection(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "route": "/employees/new",
        "title": "Novo funcionário",
        "landmarks": [{"role": "heading", "label": "Novo funcionário"}],
        "fields": [{"name": "cpf", "label": "CPF", "required": True, "visible": True}],
        "incomplete": False,
    }
    data.update(overrides)
    return data


def required(value: bool) -> FieldPropertyFact:
    return FieldPropertyFact(
        type="field_property", subject="employee", field="cpf", attribute="required", value=value
    )


def test_current_observation_verifies_supported_attribute() -> None:
    assert check(required(True), projection()) == "match"
    assert check(required(False), projection()) == "mismatch"
    screen = ScreenFact.model_validate(
        {
            "type": "screen",
            "route": "/employees/new",
            "landmarks": [{"role": "heading", "label": "Novo funcionário"}],
        }
    )
    assert check(screen, projection()) == "match"


def test_old_run_observation_cannot_verify() -> None:
    # A regra de run fica na fachada; aqui uma rota diferente nunca comprova a tela.
    screen = ScreenFact.model_validate({"type": "screen", "route": "/employees", "landmarks": []})
    assert check(screen, projection()) == "mismatch"


def test_incomplete_projection_cannot_prove_absence() -> None:
    screen = ScreenFact.model_validate(
        {
            "type": "screen",
            "route": "/employees/new",
            "landmarks": [{"role": "navigation", "label": "Menu"}],
        }
    )
    assert check(screen, projection(incomplete=True)) == "inconclusive"
    assert check(screen, projection()) == "mismatch"
    assert check(required(True), projection(fields=[], incomplete=True)) == "inconclusive"


def test_contested_remains_contested() -> None:
    # O verificador só compara; manter o estado contestado é responsabilidade da fachada.
    assert check(required(True), projection()) == "match"


def test_unsupported_fact_requires_human() -> None:
    fmt = FieldPropertyFact(
        type="field_property", subject="employee", field="cpf", attribute="format", value="cpf"
    )
    experience = ExperienceFact.model_validate(
        {
            "type": "experience",
            "attempt": "create",
            "outcome": "failure",
            "context": {"route": "/employees/new", "observation_id": "obs_1"},
        }
    )
    assert check(fmt, projection()) == "unsupported"
    assert check(experience, projection()) == "unsupported"
