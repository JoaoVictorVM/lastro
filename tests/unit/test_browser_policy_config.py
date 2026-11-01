import pytest
from pydantic import ValidationError

from lastro.config.policy import BrowserPolicy


@pytest.mark.parametrize(
    "data",
    [
        {"version": 2},
        {"rules": [{"id": "x", "effect": "allow", "match": {"entities": []}}]},
        {"auxiliary_origins": ["http://example.com"]},
        {"auxiliary_origins": ["https://example.com/path"]},
        {"auxiliary_origins": ["https://user:pass@example.com"]},
        {"preauthorized_records": [{"tenant": "*", "entity": "employee", "id": "1"}]},
        {"rules": [{"id": "x", "effect": "allow"}, {"id": "x", "effect": "deny"}]},
        {
            "bindings": [
                {
                    "id": "x",
                    "entity": "employee",
                    "operation": "update",
                    "route": "/employees/{id}",
                    "target": {"label": "Save"},
                }
            ]
        },
    ],
)
def test_invalid_policy_rejected(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        BrowserPolicy.model_validate(data)


def test_explicit_policy_roundtrip() -> None:
    policy = BrowserPolicy.model_validate(
        {
            "version": 1,
            "auxiliary_origins": ["https://keys.example.com"],
            "preauthorized_records": [{"tenant": "acme", "entity": "employee", "id": "1"}],
            "bindings": [
                {
                    "id": "edit",
                    "entity": "employee",
                    "operation": "update",
                    "route": "/employees/{id}",
                    "target": {"role": "button", "name": "Save"},
                    "fields": {"email": "email"},
                    "request": {"method": "PATCH", "path": "/api/employees/{id}"},
                }
            ],
        }
    )
    assert BrowserPolicy.model_validate(policy.model_dump()) == policy
