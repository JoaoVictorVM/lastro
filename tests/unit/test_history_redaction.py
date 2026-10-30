import hashlib
import json

import pytest

from lastro.history import InvalidEventError
from lastro.history.redaction import (
    REDACTED,
    canonical_json,
    is_sensitive_key,
    preview,
    redact,
    redact_text,
    redact_to_json,
)


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "Password",
        "user_password",
        "PASSWD",
        "client-secret",
        "access_token",
        "Authorization",
        "Cookie",
        "Set-Cookie",
        "x-api-key",
        "API_KEY",
        "session_id",
        "credentials",
    ],
)
def test_sensitive_keys_ignore_case_hyphen_and_underscore(key: str) -> None:
    assert is_sensitive_key(key)
    assert redact({key: "valor-secreto"}) == {key: REDACTED}


@pytest.mark.parametrize("key", ["subtype", "tenant_id", "url", "selector", "name"])
def test_regular_keys_are_kept(key: str) -> None:
    assert redact({key: "aurora"}) == {key: "aurora"}


def test_redaction_is_recursive() -> None:
    value = {
        "headers": {"Authorization": "Bearer abc.def.ghi", "Accept": "text/html"},
        "steps": [{"fill": {"field": "senha", "password": "hunter2"}}],
    }

    assert redact(value) == {
        "headers": {"Authorization": REDACTED, "Accept": "text/html"},
        "steps": [{"fill": {"field": "senha", "password": REDACTED}}],
    }


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("Authorization: Bearer eyJhbGciOi.payload.sig", "eyJhbGciOi.payload.sig"),
        ("use bearer abcdefghijklmnop now", "abcdefghijklmnop"),
        ("Cookie: sid=abc123; theme=dark", "abc123"),
        ("set-cookie: session=zz9xx; Path=/", "zz9xx"),
        ("https://alvo.test/api?page=2&access_token=tok123&x=1", "tok123"),
        ("POST /login password=hunter2", "hunter2"),
        ('{"client_secret": "s3cr3t"}', "s3cr3t"),
    ],
)
def test_text_patterns_are_redacted(text: str, secret: str) -> None:
    redacted = redact_text(text)

    assert secret not in redacted
    assert REDACTED in redacted


def test_plain_text_is_unchanged() -> None:
    for text in ["basic info", "session_refreshed", "tenant: aurora", "Clique em Salvar"]:
        assert redact_text(text) == text


def test_preview_and_hash_derive_from_same_redacted_canonical_json() -> None:
    arguments = {"z": 1, "password": "hunter2", "a": "x" * 500}

    result = redact_to_json(arguments)

    assert result.canonical == canonical_json(result.value)
    assert json.loads(result.canonical) == {"a": "x" * 500, "password": REDACTED, "z": 1}
    assert result.sha256 == hashlib.sha256(result.canonical.encode("utf-8")).hexdigest()
    assert len(result.preview) == 200
    assert result.preview.startswith('{"a":"xxx')
    assert result.preview.endswith("...")
    assert "hunter2" not in result.canonical


def test_hash_does_not_depend_on_secret_value() -> None:
    first = redact_to_json({"password": "um"})
    second = redact_to_json({"password": "outro"})

    assert first.sha256 == second.sha256


def test_canonical_json_is_key_ordered_and_compact() -> None:
    assert canonical_json({"b": 1, "a": [True, None]}) == '{"a":[true,null],"b":1}'


def test_short_preview_is_kept_whole() -> None:
    assert preview("curto") == "curto"


@pytest.mark.parametrize("value", [{1: "x"}, {"x": {1, 2}}, float("nan"), object()])
def test_non_json_values_are_rejected(value: object) -> None:
    with pytest.raises(InvalidEventError):
        redact(value)
