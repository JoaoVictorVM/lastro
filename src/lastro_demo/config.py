"""Configuração do demo: state_dir, segredo de sessão, credenciais e semente do sorteio."""

import io
import json
import os
import secrets
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from lastro_demo import DEFAULT_HOST, DEFAULT_PORT

PACKAGE = "lastro_demo"
ENV_EXAMPLE = ".env.example"
SEED_DIR = "seed"

STATE_DIR_ENV = "LASTRO_DEMO_STATE_DIR"
PORT_ENV = "LASTRO_DEMO_PORT"
HOST_ENV = "LASTRO_DEMO_HOST"
SESSION_SECRET_ENV = "LASTRO_DEMO_SESSION_SECRET"
SEED_ENV = "LASTRO_DEMO_SEED"
DEFAULT_RANDOM_SEED = 20240611

DB_FILENAME = "demo.sqlite3"
ACCESS_LOG_FILENAME = "access.log"


def read_package_text(*parts: str) -> str:
    return resources.files(PACKAGE).joinpath(*parts).read_text(encoding="utf-8")


def read_seed_json(name: str) -> Any:
    return json.loads(read_package_text(SEED_DIR, name))


@cache
def env_example_defaults() -> dict[str, str]:
    values = dotenv_values(stream=io.StringIO(read_package_text(ENV_EXAMPLE)))
    return {key: value for key, value in values.items() if value is not None}


@dataclass(frozen=True)
class SeedUser:
    key: str
    display_name: str
    clinics: tuple[str, ...]
    user_env: str
    password_env: str
    announce: bool
    email: str
    password: str


def load_seed_users(env: Mapping[str, str]) -> tuple[SeedUser, ...]:
    """Usuários de teste do seed; o ambiente prevalece sobre o `.env.example` do pacote."""
    defaults = env_example_defaults()
    users: list[SeedUser] = []
    for raw in read_seed_json("clinics.json")["users"]:
        user_env = str(raw["user_env"])
        password_env = str(raw["password_env"])
        users.append(
            SeedUser(
                key=str(raw["key"]),
                display_name=str(raw["display_name"]),
                clinics=tuple(str(item) for item in raw["clinics"]),
                user_env=user_env,
                password_env=password_env,
                announce=bool(raw["announce"]),
                email=env.get(user_env) or defaults[user_env],
                password=env.get(password_env) or defaults[password_env],
            )
        )
    return tuple(users)


def announced_credential_env_vars() -> tuple[str, ...]:
    names: list[str] = []
    for raw in read_seed_json("clinics.json")["users"]:
        if raw["announce"]:
            names.extend((str(raw["user_env"]), str(raw["password_env"])))
    return tuple(names)


@dataclass(frozen=True)
class DemoConfig:
    state_dir: Path
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    session_secret: str = field(default_factory=lambda: secrets.token_hex(32))
    random_seed: int = DEFAULT_RANDOM_SEED
    users: tuple[SeedUser, ...] = field(default_factory=lambda: load_seed_users(os.environ))

    @property
    def db_path(self) -> Path:
        return self.state_dir / DB_FILENAME

    @property
    def access_log_path(self) -> Path:
        return self.state_dir / ACCESS_LOG_FILENAME

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str],
        *,
        state_dir: Path | None = None,
        host: str | None = None,
        port: int | None = None,
    ) -> "DemoConfig":
        resolved_dir = state_dir
        if resolved_dir is None and env.get(STATE_DIR_ENV):
            resolved_dir = Path(env[STATE_DIR_ENV])
        if resolved_dir is None:
            resolved_dir = Path(tempfile.mkdtemp(prefix="lastro-demo-"))
        return cls(
            state_dir=resolved_dir,
            host=host or env.get(HOST_ENV) or DEFAULT_HOST,
            port=port if port is not None else int(env.get(PORT_ENV) or DEFAULT_PORT),
            session_secret=env.get(SESSION_SECRET_ENV) or secrets.token_hex(32),
            random_seed=int(env.get(SEED_ENV) or DEFAULT_RANDOM_SEED),
            users=load_seed_users(env),
        )
