from lastro.browser.policy import Action, PolicyEngine
from lastro.config.schema import Profile


def engine() -> PolicyEngine:
    profile = Profile.model_validate(
        {
            "target_url": "https://example.com",
            "login_url": "https://example.com/login",
            "tenant_id": "acme",
            "allowed_entities": ["employee"],
            "editable_fields": {"employee": ["email"]},
            "credentials": {"username_env": "USER", "password_env": "PASS"},
            "auth": {
                "verification": [
                    {"type": "api", "endpoint": "/api/me", "tenant_json_path": "tenant.id"}
                ]
            },
            "policy": {"rules": [{"id": "all", "effect": "allow"}]},
        }
    )
    return PolicyEngine(profile)


def test_editable_fields_and_unknown_origin() -> None:
    policy = engine()
    assert (
        policy.decide(Action("update", "employee", "1", {"salary": "100"}), True).reason
        == "field_not_editable"
    )
    action = Action("update", "employee", "1", {"email": "a@example.com"})
    first = policy.decide(action, True)
    assert first.effect == "require_approval"
    assert len(first.fingerprint) == 64
    assert policy.decide(action, True).fingerprint == first.fingerprint
    assert policy.decide(Action("delete", "employee", "1"), True).effect == "deny"


def test_auth_session_and_write_lock() -> None:
    policy = engine()
    action = Action("create", "employee", values={"email": "a@example.com"})
    assert policy.decide(action, False).effect == "deny"
    policy.locks.update({"tenant", "limits"})
    policy.locks.discard("tenant")
    assert policy.decide(action, True).reason == "write_lock"
    assert policy.decide(Action("read"), False).effect == "allow"


def test_destructive_name() -> None:
    policy = engine()
    assert policy.destructive("Inativar funcionário")
    assert not policy.destructive("Excluiria")
