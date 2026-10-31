import pickle

import pytest

from lastro.auth import (
    LoginCredentials,
    MissingCredentialError,
    missing_credential_message,
    resolve_credentials,
)
from lastro.config.schema import Credentials, Profile

PASSWORD = "senha-sentinela-credencial-42"
USERNAME = "usuario.sentinela@example.test"


def _profile() -> Profile:
    return Profile(
        name="homolog",
        target_url="https://homolog.example.test",
        login_url="https://homolog.example.test/login",
        tenant_id="aurora",
        credentials=Credentials(
            username_env="LASTRO_HOMOLOG_USER", password_env="LASTRO_HOMOLOG_PASSWORD"
        ),
        allowed_entities=["employee"],
    )


def test_resolves_values_from_named_variables() -> None:
    credentials = resolve_credentials(
        _profile(),
        {"LASTRO_HOMOLOG_USER": USERNAME, "LASTRO_HOMOLOG_PASSWORD": PASSWORD, "OTHER": "x"},
    )

    assert credentials.username == USERNAME
    assert credentials.password == PASSWORD


@pytest.mark.parametrize(
    ("environ", "missing"),
    [
        ({"LASTRO_HOMOLOG_USER": USERNAME}, "LASTRO_HOMOLOG_PASSWORD"),
        (
            {"LASTRO_HOMOLOG_USER": USERNAME, "LASTRO_HOMOLOG_PASSWORD": ""},
            "LASTRO_HOMOLOG_PASSWORD",
        ),
        ({"LASTRO_HOMOLOG_PASSWORD": PASSWORD}, "LASTRO_HOMOLOG_USER"),
    ],
)
def test_missing_credential_names_only_variable(environ: dict[str, str], missing: str) -> None:
    with pytest.raises(MissingCredentialError) as caught:
        resolve_credentials(_profile(), environ)

    error = caught.value
    assert error.variable == missing
    assert error.message == (
        f"Credential variable {missing} is not set. Add it to .env (see .env.example)."
    )
    assert str(error) == error.message
    assert error.reason_code == "credential_missing"
    assert PASSWORD not in repr(error)
    assert USERNAME not in repr(error)


def test_prd_message_for_homolog_password() -> None:
    assert missing_credential_message("LASTRO_HOMOLOG_PASSWORD") == (
        "Credential variable LASTRO_HOMOLOG_PASSWORD is not set. Add it to .env (see .env.example)."
    )


def test_credentials_never_render_or_serialize_values() -> None:
    credentials = LoginCredentials(username=USERNAME, password=PASSWORD)

    assert PASSWORD not in repr(credentials)
    assert PASSWORD not in str(credentials)
    assert USERNAME not in repr(credentials)
    with pytest.raises(TypeError):
        pickle.dumps(credentials)


def test_reads_process_environment_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LASTRO_HOMOLOG_USER", USERNAME)
    monkeypatch.setenv("LASTRO_HOMOLOG_PASSWORD", PASSWORD)

    assert resolve_credentials(_profile()).password == PASSWORD
