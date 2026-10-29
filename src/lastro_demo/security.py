"""Hash de senha, sessão e dependências de usuário logado e tenant ativo."""

import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass
from functools import cache

from starlette.requests import Request

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32

SESSION_USER = "user_id"
SESSION_TENANT = "tenant"
SESSION_GENERATION = "generation"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


@cache
def seed_password_hash(password: str) -> str:
    # O reset recria os usuários a cada chamada; reaproveitar o hash mantém o reset rápido.
    return hash_password(password)


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = encoded.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    candidate = hashlib.scrypt(
        password.encode("utf-8"),
        salt=bytes.fromhex(salt_hex),
        n=int(n),
        r=int(r),
        p=int(p),
        dklen=len(digest_hex) // 2,
    )
    return hmac.compare_digest(candidate.hex(), digest_hex)


@dataclass(frozen=True)
class User:
    id: int
    email: str
    display_name: str
    clinics: tuple[str, ...]


@dataclass(frozen=True)
class Tenant:
    id: str
    name: str


class LoginRequired(Exception):
    pass


class TenantRequired(Exception):
    pass


def load_user(conn: sqlite3.Connection, user_id: int) -> User | None:
    row = conn.execute(
        "SELECT id, email, display_name FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if row is None:
        return None
    clinics = conn.execute(
        "SELECT clinic_id FROM user_clinics WHERE user_id = ? ORDER BY clinic_id", (user_id,)
    ).fetchall()
    return User(
        id=int(row["id"]),
        email=str(row["email"]),
        display_name=str(row["display_name"]),
        clinics=tuple(str(item["clinic_id"]) for item in clinics),
    )


def authenticate(conn: sqlite3.Connection, email: str, password: str) -> User | None:
    row = conn.execute(
        "SELECT id, password_hash FROM users WHERE lower(email) = lower(?)", (email.strip(),)
    ).fetchone()
    if row is None or not verify_password(password, str(row["password_hash"])):
        return None
    return load_user(conn, int(row["id"]))


def load_tenant(conn: sqlite3.Connection, tenant_id: str) -> Tenant | None:
    row = conn.execute("SELECT id, name FROM clinics WHERE id = ?", (tenant_id,)).fetchone()
    return None if row is None else Tenant(id=str(row["id"]), name=str(row["name"]))


def start_session(request: Request, user: User, generation: int) -> None:
    request.session.clear()
    request.session[SESSION_USER] = user.id
    request.session[SESSION_GENERATION] = generation
    if len(user.clinics) == 1:
        request.session[SESSION_TENANT] = user.clinics[0]


def session_user(request: Request, conn: sqlite3.Connection, generation: int) -> User | None:
    """Usuário da sessão; sessões de uma geração anterior ao último reset são descartadas."""
    user_id = request.session.get(SESSION_USER)
    if not isinstance(user_id, int) or request.session.get(SESSION_GENERATION) != generation:
        request.session.clear()
        return None
    user = load_user(conn, user_id)
    if user is None:
        request.session.clear()
    return user


def session_tenant(request: Request, conn: sqlite3.Connection, user: User) -> Tenant | None:
    tenant_id = request.session.get(SESSION_TENANT)
    if not isinstance(tenant_id, str) or tenant_id not in user.clinics:
        return None
    return load_tenant(conn, tenant_id)
