"""Registro de artefatos: paths seguros dentro da run, escrita atômica, tamanho e SHA-256."""

import errno
import hashlib
import sqlite3
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from lastro.history.errors import ArtifactExistsError, DiskFullError, UnsafeArtifactPathError
from lastro.history.layout import write_chunks_atomically
from lastro.history.models import ArtifactRecord, ArtifactType

CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class StoredFile:
    relative_path: str
    path: Path
    size_bytes: int
    sha256: str


def normalize_relative_path(relative: str) -> str:
    """Aceita apenas paths relativos em forma POSIX, sem `.`, `..`, drive ou barra invertida."""
    if not relative or relative != relative.strip():
        raise UnsafeArtifactPathError("path de artefato vazio ou com espaços nas pontas")
    if "\\" in relative or ":" in relative or "\x00" in relative:
        raise UnsafeArtifactPathError(f"path de artefato inválido: {relative}")
    if PurePosixPath(relative).is_absolute():
        raise UnsafeArtifactPathError(f"path de artefato absoluto: {relative}")
    segments = relative.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise UnsafeArtifactPathError(f"path de artefato não normalizado: {relative}")
    return relative


def parse_artifact_type(value: ArtifactType | str) -> ArtifactType:
    try:
        return ArtifactType(value)
    except ValueError:
        raise UnsafeArtifactPathError(f"tipo de artefato desconhecido: {value}") from None


def resolve_inside(run_dir: Path, relative: str) -> Path:
    """Path final dentro da run; symlinks que escapem da pasta da run são rejeitados."""
    target = run_dir.joinpath(*normalize_relative_path(relative).split("/"))
    base = run_dir.resolve()
    if not target.resolve().is_relative_to(base):
        raise UnsafeArtifactPathError(f"path de artefato fora da pasta da run: {relative}")
    return target


def file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _source_chunks(source: Path) -> Iterator[bytes]:
    with source.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            yield chunk


def store_file(
    run_dir: Path, relative: str, *, content: bytes | None = None, source: Path | None = None
) -> StoredFile:
    """Grava um arquivo novo e imutável na run e mede o resultado já no destino final."""
    if content is not None and source is None:
        chunks: Iterable[bytes] = (content,)
    elif source is not None and content is None:
        if not source.is_file():
            raise ValueError(f"arquivo de origem não encontrado: {source}")
        chunks = _source_chunks(source)
    else:
        raise ValueError("informe content ou source, exatamente um")
    target = resolve_inside(run_dir, relative)
    if target.exists() or target.is_symlink():
        raise ArtifactExistsError(f"artefato já existe: {relative}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        write_chunks_atomically(target, chunks)
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise DiskFullError(relative) from None
        raise
    size, sha256 = file_digest(target)
    return StoredFile(relative_path=relative, path=target, size_bytes=size, sha256=sha256)


def insert_artifact(
    conn: sqlite3.Connection,
    run_id: str,
    stored: StoredFile,
    artifact_type: ArtifactType,
    created_at: str,
) -> ArtifactRecord:
    record = ArtifactRecord(
        id=f"art_{uuid.uuid4().hex}",
        run_id=run_id,
        relative_path=stored.relative_path,
        artifact_type=artifact_type,
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
        created_at=created_at,
    )
    conn.execute(
        "INSERT INTO artifacts "
        "(id, run_id, relative_path, artifact_type, size_bytes, sha256, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            record.id,
            record.run_id,
            record.relative_path,
            record.artifact_type.value,
            record.size_bytes,
            record.sha256,
            record.created_at,
        ),
    )
    return record


def artifact_from_row(row: sqlite3.Row) -> ArtifactRecord:
    return ArtifactRecord(
        id=row["id"],
        run_id=row["run_id"],
        relative_path=row["relative_path"],
        artifact_type=ArtifactType(row["artifact_type"]),
        size_bytes=row["size_bytes"],
        sha256=row["sha256"],
        created_at=row["created_at"],
    )


def list_artifacts(conn: sqlite3.Connection, run_id: str) -> list[ArtifactRecord]:
    rows = conn.execute(
        "SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_at, relative_path", (run_id,)
    ).fetchall()
    return [artifact_from_row(row) for row in rows]
