from typing import Any

import pytest
from pydantic import ValidationError

from lastro.config.policy import RequestBinding
from lastro.config.schema import LastroConfig
from lastro.config.testdata import EntityTestData

CREATE = {
    "id": "employee-create",
    "entity": "employee",
    "operation": "create",
    "route": "/employees/new",
    "target": {"role": "button", "name": "Salvar"},
    "fields": {"full_name": "full_name"},
    "request": {"method": "POST", "path": "/employees", "location_id": True},
}


def config(test_data: dict[str, Any], bindings: list[dict[str, Any]] | None = None) -> LastroConfig:
    profile: dict[str, Any] = {
        "target_url": "https://alvo.example.test",
        "login_url": "https://alvo.example.test/login",
        "tenant_id": "acme",
        "allowed_entities": ["employee"],
        "credentials": {"username_env": "HOMOLOG_USERNAME", "password_env": "HOMOLOG_PASSWORD"},
        "policy": {"bindings": [CREATE] if bindings is None else bindings},
        "test_data": test_data,
    }
    return LastroConfig.model_validate({"profiles": {"homolog": profile}})


def test_cleanup_defaults_required() -> None:
    profile = config({}).profiles["homolog"]

    settings = profile.test_data.for_entity("employee")
    assert settings.cleanup_required is True
    assert settings.detail_path is None
    assert settings.cleanup_instructions is None
    explicit = config(
        {"entities": {"employee": {"cleanup_required": False, "cleanup_instructions": "Excluir."}}}
    ).profiles["homolog"]
    assert explicit.test_data.for_entity("employee").cleanup_required is False


@pytest.mark.parametrize(
    ("entities", "bindings"),
    [
        ({"customer": {}}, None),
        ({"employee": {}}, []),
        ({"employee": {}}, [{**CREATE, "operation": "update", "route": "/employees/{id}"}]),
    ],
)
def test_entity_must_be_allowed_and_have_create_binding(
    entities: dict[str, Any], bindings: list[dict[str, Any]] | None
) -> None:
    with pytest.raises(ValidationError, match="test_data entities"):
        config({"entities": entities}, bindings)


@pytest.mark.parametrize(
    "path",
    [
        "/employees",
        "/employees/{id}/{id}",
        "/employees/{record}",
        "https://alvo.example.test/employees/{id}",
        "/employees/{id}?tab=1",
        "/employees/{id}#top",
        "/employees/../{id}",
        "employees/{id}",
        "/employees/x{id}",
    ],
)
def test_detail_path_requires_single_id_placeholder(path: str) -> None:
    with pytest.raises(ValidationError):
        EntityTestData(detail_path=path)


def test_detail_path_substitutes_safe_segment() -> None:
    settings = EntityTestData(detail_path="/employees/{id}")

    assert settings.detail_for("42") == "/employees/42"
    assert settings.detail_for("a/b") is None
    assert settings.detail_for("..") is None
    assert settings.detail_for("unknown ") is None
    assert EntityTestData().detail_for("42") is None


def test_cleanup_instructions_are_bounded() -> None:
    with pytest.raises(ValidationError):
        EntityTestData(cleanup_instructions="")
    with pytest.raises(ValidationError):
        EntityTestData(cleanup_instructions="x" * 2001)


def test_create_binding_accepts_single_identifier_rule() -> None:
    base = {"method": "POST", "path": "/employees"}

    assert RequestBinding.model_validate(base).response_id is None
    assert RequestBinding.model_validate({**base, "response_tenant": "tenant.id"})
    with pytest.raises(ValidationError, match="not both"):
        RequestBinding.model_validate({**base, "response_id": "id", "location_id": True})
    with pytest.raises(ValidationError):
        RequestBinding.model_validate({**base, "response_tenant": "tenant..id"})
