"""Storage state privado por perfil em `<project_data_dir>/.auth/<profile>.json`.

O conteúdo (cookies e storage de origem no formato do Playwright) é opaco fora deste módulo,
nunca entra no histórico e só é publicado depois de um tenant verificado.
"""

import contextlib
import json
import os
import secrets
import stat
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from lastro.auth import winacl
from lastro.auth.models import AuthenticatedSessionRef, SessionRefError
from lastro.config.schema import is_valid_profile_name
from lastro.timeutil import utc_now

AUTH_DIRNAME = ".auth"
STATE_SUFFIX = ".json"
TEMP_SUFFIX = ".tmp"
MAX_STATE_AGE = timedelta(hours=8)
POSIX_DIR_MODE = 0o700
POSIX_FILE_MODE = 0o600

type Clock = Callable[[], datetime]
type StorageStateData = dict[str, Any]


class StorageProtectionError(OSError):
    pass


@dataclass(frozen=True)
class LoadedState:
    ref: AuthenticatedSessionRef
    state: StorageStateData = field(repr=False)


def auth_dir(project_data_dir: Path) -> Path:
    return project_data_dir / AUTH_DIRNAME


def state_path(project_data_dir: Path, profile_name: str) -> Path:
    """Path do estado do perfil, comprovadamente descendente de `.auth`."""
    if not is_valid_profile_name(profile_name):
        raise SessionRefError("invalid_profile")
    base = auth_dir(project_data_dir)
    path = base / f"{profile_name}{STATE_SUFFIX}"
    if not path.resolve().is_relative_to(base.resolve()):
        raise SessionRefError("outside_auth_dir")
    return path


def is_valid_state(data: object) -> bool:
    if not isinstance(data, dict):
        return False
    mapping = cast(dict[str, object], data)
    cookies = mapping.get("cookies")
    origins = mapping.get("origins")
    if not isinstance(cookies, list) or not isinstance(origins, list):
        return False
    items = [*cast(list[object], cookies), *cast(list[object], origins)]
    return all(isinstance(item, dict) for item in items)


def _ensure_private_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        winacl.create_owner_only_directory(path)
        if not winacl.is_owner_only(path):
            raise StorageProtectionError(f"não foi possível proteger {AUTH_DIRNAME}")
        return
    path.mkdir(mode=POSIX_DIR_MODE, exist_ok=True)
    os.chmod(path, POSIX_DIR_MODE)


def _create_private_file(path: Path) -> None:
    """Cria o arquivo vazio já restrito ao usuário atual, antes de receber conteúdo."""
    if sys.platform == "win32":
        winacl.create_owner_only_file(path)
        if not winacl.is_owner_only(path):
            raise StorageProtectionError("não foi possível proteger o storage state temporário")
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, POSIX_FILE_MODE)
    try:
        os.fchmod(descriptor, POSIX_FILE_MODE)
    finally:
        os.close(descriptor)


def _protect_final(path: Path) -> None:
    if sys.platform == "win32":
        winacl.apply_owner_only(path, directory=False)
        if not winacl.is_owner_only(path):
            raise StorageProtectionError("não foi possível proteger o storage state")
        return
    os.chmod(path, POSIX_FILE_MODE)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != POSIX_FILE_MODE:
        raise StorageProtectionError("não foi possível proteger o storage state")


class SessionStateStore:
    """Ciclo de vida do storage state de um projeto: carga, validade, publicação e remoção."""

    def __init__(self, project_data_dir: Path, *, clock: Clock = utc_now) -> None:
        self.project_data_dir = project_data_dir
        self._clock = clock

    @property
    def directory(self) -> Path:
        return auth_dir(self.project_data_dir)

    def path_for(self, profile_name: str) -> Path:
        return state_path(self.project_data_dir, profile_name)

    def _ref(self, profile_name: str, path: Path, created_at: datetime) -> AuthenticatedSessionRef:
        return AuthenticatedSessionRef(
            profile_name=profile_name,
            path=path.resolve(),
            created_at=created_at,
            expires_at=created_at + MAX_STATE_AGE,
        )

    def _created_at(self, path: Path) -> datetime:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC)

    def load(self, profile_name: str) -> LoadedState | None:
        """Estado reutilizável ou None; expirado, ilegível ou inválido é removido."""
        path = self.path_for(profile_name)
        try:
            created_at = self._created_at(path)
        except FileNotFoundError:
            return None
        ref = self._ref(profile_name, path, created_at)
        if ref.is_expired(self._clock()):
            self.delete(profile_name)
            return None
        try:
            data: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            self.delete(profile_name)
            return None
        if not is_valid_state(data):
            self.delete(profile_name)
            return None
        return LoadedState(ref=ref, state=cast(StorageStateData, data))

    def publish(self, profile_name: str, state: Mapping[str, object]) -> AuthenticatedSessionRef:
        """Grava em temporário protegido no mesmo diretório e troca atomicamente."""
        if not is_valid_state(state):
            raise ValueError("storage state com estrutura inválida")
        path = self.path_for(profile_name)
        _ensure_private_dir(path.parent)
        temporary = path.parent / f".{profile_name}.{secrets.token_hex(8)}{TEMP_SUFFIX}"
        _create_private_file(temporary)
        try:
            with temporary.open("r+b") as handle:
                handle.write(json.dumps(state).encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise
        _protect_final(path)
        created_at = self._clock()
        stamp = created_at.timestamp()
        os.utime(path, (stamp, stamp))
        return self._ref(profile_name, path, created_at)

    def delete(self, profile_name: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path_for(profile_name).unlink()

    def validate_ref(self, ref: AuthenticatedSessionRef, profile_name: str) -> None:
        """Rejeita referência vencida, de outro perfil, fora de `.auth` ou de arquivo inválido."""
        if ref.profile_name != profile_name:
            raise SessionRefError("profile_mismatch")
        expected = self.path_for(profile_name).resolve()
        if ref.path != expected:
            raise SessionRefError("outside_auth_dir")
        if ref.is_expired(self._clock()):
            raise SessionRefError("expired")
        try:
            created_at = self._created_at(expected)
        except FileNotFoundError:
            raise SessionRefError("missing") from None
        if abs((created_at - ref.created_at).total_seconds()) > 1:
            raise SessionRefError("replaced")
        if self.load(profile_name) is None:
            raise SessionRefError("invalid")

    def read_for_context(self, ref: AuthenticatedSessionRef, profile_name: str) -> StorageStateData:
        """Conteúdo do estado para abrir um BrowserContext; só depois de validar a referência."""
        self.validate_ref(ref, profile_name)
        loaded = self.load(profile_name)
        if loaded is None:
            raise SessionRefError("invalid")
        return loaded.state
