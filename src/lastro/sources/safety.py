"""Política documental: inventário seguro de uma raiz cadastrada, leitura sem links e scanner.

Reaproveita as negações e o leitor da fronteira de código, mas com política própria: PDF é
elegível aqui e continua negado para leitura de código. Nada é aberto antes de o caminho e os
metadados passarem; possível segredo exclui o documento inteiro, sem persistir texto nem hash.
"""

import hashlib
import os
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from lastro.change_context.source import (
    DENIED_COMPONENTS,
    ESCAPE,
    OVERSIZED,
    REPARSE_POINT,
    SPECIAL_FILE,
    SYMLINK,
    UNSTABLE,
    SafeReader,
    UnsafeFile,
    is_denied_name,
    is_safe_relative,
)
from lastro.sources.models import (
    DENIED_PATTERN,
    MAX_BYTES,
    MAX_FILES,
    POSSIBLE_SECRET,
    SCANNER_UNAVAILABLE,
    SOURCE_UNAVAILABLE,
    UNSAFE_PATH,
    UNSTABLE_FILE,
    DocumentFormat,
    SourcesError,
    document_format,
)

# Lote do scanner: limita o stdin de cada execução sem abrir um processo por documento.
SCAN_BATCH_CHARACTERS = 4_000_000


@dataclass(frozen=True)
class Candidate:
    relative_path: str
    format: DocumentFormat
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class Exclusion:
    relative_path: str
    reason: str


@dataclass(frozen=True)
class Inventory:
    candidates: tuple[Candidate, ...]
    exclusions: tuple[Exclusion, ...]

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.candidates)


def _is_reparse(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or _is_reparse(info)


def canonical_root(path: Path) -> Path:
    """Diretório local existente, resolvido; a própria raiz não pode ser link."""
    absolute = Path(os.path.abspath(path))
    try:
        info = os.lstat(absolute)
    except OSError:
        raise SourcesError(SOURCE_UNAVAILABLE, "A raiz documental não existe.") from None
    if _is_link(info):
        raise SourcesError(UNSAFE_PATH, "A raiz documental não pode ser link ou junction.")
    if not stat.S_ISDIR(info.st_mode):
        raise SourcesError(SOURCE_UNAVAILABLE, "A raiz documental precisa ser um diretório.")
    return Path(os.path.realpath(absolute))


def root_key(root: Path) -> str:
    """Identidade da raiz respeitando a sensibilidade a caixa do volume."""
    text = root.as_posix()
    if os.path.normcase("A") == os.path.normcase("a"):
        text = text.lower()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def overlaps(left: Path, right: Path) -> bool:
    def norm(path: Path) -> str:
        return os.path.normcase(str(path)).rstrip("\\/") + os.sep

    a, b = norm(left), norm(right)
    return a.startswith(b) or b.startswith(a)


def inventory(root: Path) -> Inventory:
    """Metadados de todos os candidatos, sem abrir conteúdo e sem seguir links.

    Falha ao listar a raiz ou qualquer subdiretório aborta: inventário incompleto nunca
    autoriza inferir remoções.
    """
    candidates: list[Candidate] = []
    exclusions: list[Exclusion] = []
    try:
        info = os.lstat(root)
    except OSError:
        raise SourcesError(SOURCE_UNAVAILABLE, "A raiz documental está inacessível.") from None
    if _is_link(info) or not stat.S_ISDIR(info.st_mode):
        raise SourcesError(SOURCE_UNAVAILABLE, "A raiz documental está inacessível.")

    def walk(directory: Path, prefix: str) -> None:
        try:
            with os.scandir(directory) as entries:
                listed = sorted(entries, key=lambda entry: entry.name)
        except OSError:
            raise SourcesError(
                SOURCE_UNAVAILABLE, "Inventário incompleto: um diretório não pôde ser listado."
            ) from None
        for entry in listed:
            relative = f"{prefix}{entry.name}"
            try:
                meta = entry.stat(follow_symlinks=False)
            except OSError:
                raise SourcesError(
                    SOURCE_UNAVAILABLE, "Inventário incompleto: metadados indisponíveis."
                ) from None
            if stat.S_ISDIR(meta.st_mode) and not _is_link(meta):
                if entry.name.lower() in DENIED_COMPONENTS:
                    continue
                walk(Path(entry.path), relative + "/")
                continue
            kind = document_format(relative)
            if kind is None and not _is_link(meta):
                continue
            if not is_safe_relative(relative) or _is_link(meta):
                exclusions.append(Exclusion(relative, UNSAFE_PATH))
                continue
            if kind is None:
                continue
            if not stat.S_ISREG(meta.st_mode):
                exclusions.append(Exclusion(relative, UNSAFE_PATH))
                continue
            if is_denied_name(relative):
                exclusions.append(Exclusion(relative, DENIED_PATTERN))
                continue
            candidates.append(Candidate(relative, kind, meta.st_size, meta.st_mtime_ns))

    walk(root, "")
    return Inventory(tuple(candidates), tuple(exclusions))


def check_limits(file_count: int, total_bytes: int) -> str | None:
    """Limites inclusivos do corpus agregado do projeto."""
    if file_count > MAX_FILES:
        return f"{file_count} arquivos elegíveis excedem o limite de {MAX_FILES}"
    if total_bytes > MAX_BYTES:
        return f"{total_bytes} bytes excedem o limite de {MAX_BYTES}"
    return None


_READ_REASONS = {
    SYMLINK: UNSAFE_PATH,
    REPARSE_POINT: UNSAFE_PATH,
    ESCAPE: UNSAFE_PATH,
    SPECIAL_FILE: UNSAFE_PATH,
    UNSTABLE: UNSTABLE_FILE,
    OVERSIZED: UNSTABLE_FILE,
}


class FileRejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class DocumentReader:
    """Leitura revalidada antes e depois; arquivo que muda durante a leitura é instável."""

    def __init__(self, root: Path) -> None:
        self._reader = SafeReader(root, max_bytes=MAX_BYTES)

    def read(self, candidate: Candidate) -> bytes:
        try:
            data = self._reader.read(candidate.relative_path)
        except UnsafeFile as exc:
            raise FileRejected(_READ_REASONS.get(exc.reason, UNSAFE_PATH)) from None
        except FileNotFoundError:
            raise FileRejected(UNSTABLE_FILE) from None
        except OSError:
            raise FileRejected(UNSTABLE_FILE) from None
        return data


class SecretScanner(Protocol):
    def findings(self, texts: Sequence[str]) -> list[set[int]]: ...


type ScannerFactory = Callable[[], SecretScanner]


def gitleaks_scanner() -> SecretScanner:
    """Scanner real com autoteste; ausência ou falha fecham o sync antes de publicar."""
    from lastro.change_context.scanner import Gitleaks, ScannerError

    scanner = Gitleaks()
    try:
        scanner.ensure()
    except ScannerError:
        raise SourcesError(
            SCANNER_UNAVAILABLE,
            "gitleaks indisponível ou incompatível; o sync não publica sem o scanner.",
        ) from None
    return scanner


def flag_secrets(scanner: SecretScanner, texts: Sequence[str]) -> list[bool]:
    """True para cada texto com possível segredo; falha do scanner fecha tudo."""
    from lastro.change_context.scanner import ScannerError

    flags: list[bool] = []
    batch: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal size
        if not batch:
            return
        try:
            found = scanner.findings(batch)
        except ScannerError:
            raise SourcesError(
                SCANNER_UNAVAILABLE, "gitleaks falhou durante a varredura; nada foi publicado."
            ) from None
        flags.extend(bool(lines) for lines in found)
        batch.clear()
        size = 0

    for text in texts:
        if batch and size + len(text) > SCAN_BATCH_CHARACTERS:
            flush()
        batch.append(text)
        size += len(text)
    flush()
    return flags


__all__ = [
    "POSSIBLE_SECRET",
    "Candidate",
    "DocumentReader",
    "Exclusion",
    "FileRejected",
    "Inventory",
    "ScannerFactory",
    "SecretScanner",
    "canonical_root",
    "check_limits",
    "flag_secrets",
    "gitleaks_scanner",
    "inventory",
    "overlaps",
    "root_key",
]
