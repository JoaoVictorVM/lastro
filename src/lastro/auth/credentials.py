"""Fronteira de segredos: valores de credencial só existem dentro do login determinístico."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import NoReturn

from lastro.auth.models import MissingCredentialError
from lastro.config.schema import Profile


@dataclass(frozen=True)
class LoginCredentials:
    username: str = field(repr=False)
    password: str = field(repr=False)

    def __repr__(self) -> str:
        return "LoginCredentials(<redacted>)"

    def __reduce__(self) -> NoReturn:
        raise TypeError("LoginCredentials não pode ser serializado")


def resolve_credentials(
    profile: Profile, environ: Mapping[str, str] | None = None
) -> LoginCredentials:
    """Lê os valores das variáveis nomeadas no perfil; a falha cita apenas o nome ausente."""
    env = environ if environ is not None else os.environ
    values: list[str] = []
    for name in profile.credential_variable_names():
        value = env.get(name)
        if not value:
            raise MissingCredentialError(name)
        values.append(value)
    username, password = values
    return LoginCredentials(username=username, password=password)
