from typing import Any

import pytest
from pydantic import ValidationError

from lastro.config.observability import (
    EvaluationContextError,
    EvaluationTraceContext,
    ObservabilitySettings,
)
from lastro.config.schema import Profile
from lastro.history.runs import config_snapshot

ENDPOINT = "http://localhost:3000/api/public/otel/v1/traces"
REMOTE = "https://cloud.langfuse.com/api/public/otel/v1/traces"


def settings(**overrides: Any) -> ObservabilitySettings:
    values: dict[str, Any] = {
        "enabled": True,
        "endpoint": ENDPOINT,
        "ui_url": "http://localhost:3000",
        "langfuse_project_id": "proj-1",
    }
    values.update(overrides)
    return ObservabilitySettings.model_validate(values)


def profile(extra: dict[str, Any] | None = None) -> Profile:
    data: dict[str, Any] = {
        "target_url": "https://app.example.test",
        "login_url": "https://app.example.test/login",
        "tenant_id": "acme",
        "allowed_entities": [],
        "credentials": {"username_env": "APP_USER", "password_env": "APP_PASSWORD"},
    }
    data.update(extra or {})
    return Profile.model_validate(data)


def test_missing_block_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")

    current = profile().observability

    assert current.enabled is False
    assert current.destination() is None


def test_enabled_requires_explicit_destination() -> None:
    with pytest.raises(ValidationError) as error:
        ObservabilitySettings.model_validate({"enabled": True})
    assert "endpoint, ui_url, langfuse_project_id" in str(error.value)
    with pytest.raises(ValidationError):
        ObservabilitySettings.model_validate({"enabled": "true", "endpoint": ENDPOINT})
    with pytest.raises(ValidationError):
        settings(endpoint="http://localhost:3000/v1/traces")
    with pytest.raises(ValidationError):
        settings(ui_url="http://127.0.0.1:3001")
    with pytest.raises(ValidationError):
        settings(secret_key_env="LANGFUSE_PUBLIC_KEY")
    with pytest.raises(ValidationError):
        ObservabilitySettings.model_validate({"enabled": False, "headers": {"x": "y"}})


def test_remote_http_is_rejected() -> None:
    with pytest.raises(ValidationError) as error:
        settings(
            endpoint="http://langfuse.example.test/api/public/otel/v1/traces",
            ui_url="http://langfuse.example.test",
        )
    assert "https" in str(error.value)
    accepted = settings(endpoint=REMOTE, ui_url="https://cloud.langfuse.com")
    assert accepted.destination() is not None


def test_loopback_http_is_allowed() -> None:
    for host in ("localhost", "127.0.0.1", "[::1]"):
        base = f"http://{host}:3000"
        current = settings(endpoint=base + "/api/public/otel/v1/traces", ui_url=base)
        assert current.enabled


def test_userinfo_query_and_fragment_are_rejected() -> None:
    for endpoint in (
        "https://user:pass@cloud.langfuse.com/api/public/otel/v1/traces",
        "https://cloud.langfuse.com/api/public/otel/v1/traces?key=1",
        "https://cloud.langfuse.com/api/public/otel/v1/traces#frag",
    ):
        with pytest.raises(ValidationError):
            settings(endpoint=endpoint, ui_url="https://cloud.langfuse.com")
    with pytest.raises(ValidationError):
        settings(langfuse_project_id="proj/../x")


def test_snapshot_excludes_credentials(
    history_context: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-never-in-snapshot")
    current = settings()
    updated = history_context.profile.model_copy(update={"observability": current})
    context = type(history_context)(history_context.loaded, updated)

    snapshot = config_snapshot(context)

    environment = snapshot["environment"]
    assert isinstance(environment, dict)
    assert "observability" not in environment
    assert "sk-lf-never-in-snapshot" not in str(snapshot)
    destination = current.destination()
    assert destination is not None
    assert set(destination.as_json()) == {"endpoint", "ui_url", "langfuse_project_id"}


def test_invalid_evaluation_context_is_rejected() -> None:
    EvaluationTraceContext("demo-regression-01", "without-memory")
    for suite, condition in (("", "x"), ("a b", "x"), ("x" * 129, "x"), ("ok", "with/slash")):
        with pytest.raises(EvaluationContextError):
            EvaluationTraceContext(suite, condition)
    assert EvaluationTraceContext.from_columns("s", None) is None
