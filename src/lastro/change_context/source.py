"""Fronteira de arquivos: allowlist, negações e leitura segura de arquivos do working tree.

Negações são avaliadas por nome, metadados e caminho antes de qualquer abertura; a inspeção de
bytes só acontece em um candidato já permitido por caminho e tamanho, e nada dela é persistido.
"""

import os
import re
import stat
import sys
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from lastro.config.source import MAX_FILE_BYTES, is_safe_glob

DENIED_NAMES = (
    ".env*",
    "*.pem",
    "*.key",
    "secrets.*",
    # Toda a família appsettings: conferir connection strings exigiria abrir o arquivo.
    "appsettings*.json",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.kdbx",
    "*.ppk",
    "*.ovpn",
    ".netrc",
    "_netrc",
    ".npmrc",
    ".pypirc",
    ".htpasswd",
    ".git-credentials",
    "credentials",
    "credentials.*",
    "*.session",
    "storage-state*.json",
)
DENIED_COMPONENTS = frozenset({".git", ".ssh", ".aws", ".gnupg", ".docker", ".lastro"})
BINARY_EXTENSIONS = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tif", ".tiff", ".psd",
        ".pdf", ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".tar", ".jar", ".war",
        ".exe", ".dll", ".so", ".dylib", ".bin", ".o", ".a", ".lib", ".obj", ".class", ".pyc",
        ".pyd", ".whl", ".mp3", ".mp4", ".mov", ".avi", ".wav", ".ogg", ".webm", ".woff",
        ".woff2", ".ttf", ".otf", ".eot", ".sqlite", ".db", ".dat", ".iso", ".dmg", ".msi",
    }
)  # fmt: skip
SYMLINK_MODE = "120000"
SUBMODULE_MODE = "160000"
REGULAR_MODES = frozenset({"100644", "100755"})

UNSAFE_PATH = "unsafe_path"
DENIED_PATTERN = "denied_pattern"
NOT_ALLOWED = "not_allowed"
SYMLINK = "symlink"
REPARSE_POINT = "reparse_point"
SUBMODULE = "submodule"
SPECIAL_FILE = "special_file"
BINARY = "binary"
OVERSIZED = "oversized"
UNSTABLE = "unstable"
ESCAPE = "escape"
SECURITY_REASONS = frozenset({UNSAFE_PATH, SYMLINK, REPARSE_POINT, ESCAPE})

_DRIVE = re.compile(r"^[A-Za-z]:")


def is_safe_relative(path: str) -> bool:
    """Caminho relativo POSIX sem absoluto, UNC, drive, ADS, NUL, barra invertida ou '..'."""
    if not path or "\x00" in path or "\\" in path or ":" in path or _DRIVE.match(path):
        return False
    if path.startswith("/"):
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def _glob_regex(pattern: str) -> re.Pattern[str]:
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            parts.append("(?:[^/]+/)*")
            index += 3
        elif pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif pattern[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    return re.compile("^" + "".join(parts) + "$")


def is_denied_name(path: str) -> bool:
    """Negações valem sem diferenciar caixa em todas as plataformas."""
    parts = path.lower().split("/")
    if any(part in DENIED_COMPONENTS for part in parts[:-1]) or parts[-1] in DENIED_COMPONENTS:
        return True
    name = parts[-1]
    return any(fnmatchcase(name, pattern) for pattern in DENIED_NAMES)


def is_binary_name(path: str) -> bool:
    return PurePosixPath(path.lower()).suffix in BINARY_EXTENSIONS


@dataclass(frozen=True)
class SourcePolicy:
    """Allowlist case-sensitive; negações sempre antes da allowlist."""

    allow: tuple[str, ...]
    compiled: tuple[re.Pattern[str], ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.allow or not all(is_safe_glob(item) for item in self.allow):
            raise ValueError("source allowlist must be non-empty relative POSIX globs")
        object.__setattr__(self, "compiled", tuple(_glob_regex(item) for item in self.allow))

    def classify(self, path: str) -> str | None:
        """Motivo de exclusão por caminho e nome; None quando o candidato pode seguir."""
        if not is_safe_relative(path):
            return UNSAFE_PATH
        if is_denied_name(path):
            return DENIED_PATTERN
        if not any(regex.match(path) for regex in self.compiled):
            return NOT_ALLOWED
        return None

    def classify_entry(self, path: str, mode: str, size: int | None) -> str | None:
        """Política completa por metadados Git, ainda sem ler conteúdo."""
        reason = self.classify(path)
        if reason is not None:
            return reason
        if mode == SYMLINK_MODE:
            return SYMLINK
        if mode == SUBMODULE_MODE:
            return SUBMODULE
        if mode not in REGULAR_MODES:
            return SPECIAL_FILE
        if is_binary_name(path):
            return BINARY
        if size is None or size > MAX_FILE_BYTES:
            return OVERSIZED
        return None


def is_text(data: bytes) -> bool:
    """NUL ou UTF-8 inválido excluem o candidato; os bytes inspecionados nunca saem daqui."""
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


class UnsafeFile(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _is_reparse(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _check_link(info: os.stat_result) -> None:
    if stat.S_ISLNK(info.st_mode):
        raise UnsafeFile(SYMLINK)
    if _is_reparse(info):
        raise UnsafeFile(REPARSE_POINT)


class SafeReader:
    """Lê arquivos do working tree sem seguir links e revalida identidade antes e depois.

    POSIX abre cada componente relativo a um handle com O_NOFOLLOW; Windows recusa reparse
    points em todos os ancestrais e confere o caminho final. Falha em garantir essas
    propriedades exclui o candidato.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.real_root = Path(os.path.realpath(root))

    def lstat(self, relative: str) -> os.stat_result:
        """Valida todos os ancestrais e o próprio caminho sem segui-los."""
        current = self.root
        info: os.stat_result | None = None
        for part in relative.split("/"):
            current = current / part
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                raise
            except OSError:
                raise UnsafeFile(SPECIAL_FILE) from None
            _check_link(info)
        if info is None:
            raise UnsafeFile(UNSAFE_PATH)
        return info

    def read(self, relative: str, max_bytes: int = MAX_FILE_BYTES) -> bytes:
        if not is_safe_relative(relative):
            raise UnsafeFile(UNSAFE_PATH)
        before = self.lstat(relative)
        if not stat.S_ISREG(before.st_mode):
            raise UnsafeFile(SPECIAL_FILE)
        if before.st_size > max_bytes:
            raise UnsafeFile(OVERSIZED)
        data = (
            self._read_posix(relative)
            if sys.platform != "win32"
            else self._read_windows(relative, before)
        )
        after = self.lstat(relative)
        if (after.st_ino, after.st_size, after.st_mtime_ns) != (
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) or len(data) != before.st_size:
            raise UnsafeFile(UNSTABLE)
        return data

    def _read_posix(self, relative: str) -> bytes:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory = getattr(os, "O_DIRECTORY", 0)
        parts = relative.split("/")
        fd = os.open(self.root, os.O_RDONLY | directory)
        try:
            for part in parts[:-1]:
                try:
                    child = os.open(part, os.O_RDONLY | directory | nofollow, dir_fd=fd)
                except OSError:
                    raise UnsafeFile(SYMLINK) from None
                os.close(fd)
                fd = child
            try:
                handle = os.open(parts[-1], os.O_RDONLY | nofollow, dir_fd=fd)
            except OSError:
                raise UnsafeFile(SYMLINK) from None
        finally:
            os.close(fd)
        return self._drain(handle)

    def _read_windows(self, relative: str, before: os.stat_result) -> bytes:
        path = self.root.joinpath(*relative.split("/"))
        handle = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        try:
            opened = os.fstat(handle)
            if (opened.st_ino, opened.st_dev) != (before.st_ino, before.st_dev):
                raise UnsafeFile(UNSTABLE)
            final = Path(os.path.realpath(path))
            if not final.is_relative_to(self.real_root):
                raise UnsafeFile(ESCAPE)
        except BaseException:
            os.close(handle)
            raise
        return self._drain(handle)

    @staticmethod
    def _drain(handle: int) -> bytes:
        try:
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(handle, 65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    raise UnsafeFile(OVERSIZED)
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(handle)
