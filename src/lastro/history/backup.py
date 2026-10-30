"""Backup consistente: snapshot online do SQLite, pastas das runs e manifesto de hashes.

O archive é um zip com `manifest.json` na raiz e a pasta `<project_id>/` com `history.sqlite`
e `runs/`. Extraído em uma pasta vazia, essa pasta funciona como private data root.
"""

import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from lastro import get_version
from lastro.history.artifacts import CHUNK_SIZE, file_digest
from lastro.history.database import Database, db_timestamp
from lastro.history.errors import BackupError
from lastro.history.layout import DATABASE_FILENAME, HistoryLayout, temporary_sibling
from lastro.history.migrations import schema_version
from lastro.history.models import JsonValue

MANIFEST_FILENAME = "manifest.json"
BACKUP_FORMAT_VERSION = 1
ARCHIVE_SUFFIX = ".zip"


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class BackupResult:
    archive: Path
    project_id: str
    files: tuple[ManifestEntry, ...]


def default_archive_name(project_id: str, moment: datetime) -> str:
    stamp = moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"lastro-backup-{project_id}-{stamp}{ARCHIVE_SUFFIX}"


def resolve_destination(destination: Path, layout: HistoryLayout, moment: datetime) -> Path:
    """Diretório existente recebe um nome gerado; arquivo existente nunca é sobrescrito."""
    target = destination
    if destination.is_dir():
        target = destination / default_archive_name(layout.root.name, moment)
    if target.exists():
        raise BackupError(f"o destino já existe: {target}")
    if not target.parent.is_dir():
        raise BackupError(f"a pasta de destino não existe: {target.parent}")
    if target.resolve().is_relative_to(layout.root.resolve()):
        raise BackupError("o destino não pode ficar dentro da pasta do histórico do projeto")
    return target


def _snapshot_database(database: Database, target: Path) -> None:
    # A API de backup online lê um snapshot consistente mesmo com o banco aberto em WAL.
    with database.connection() as source:
        destination = sqlite3.connect(str(target))
        try:
            source.backup(destination)
            destination.execute("PRAGMA journal_mode=DELETE")
        finally:
            destination.close()


def _run_entries(runs_dir: Path) -> Iterator[tuple[Path, bool]]:
    """Diretórios e arquivos das runs; pastas vazias como evidence/ também são preservadas."""
    if not runs_dir.is_dir():
        return
    yield runs_dir, True
    for directory, dirnames, filenames in os.walk(runs_dir, followlinks=False):
        dirnames.sort()
        base = Path(directory)
        for name in dirnames:
            if not (base / name).is_symlink():
                yield base / name, True
        for name in sorted(filenames):
            path = base / name
            if path.is_symlink() or (name.startswith(".") and name.endswith(".tmp")):
                continue
            yield path, False


def _add_file(archive: zipfile.ZipFile, source: Path, name: str) -> ManifestEntry:
    digest = hashlib.sha256()
    size = 0
    info = zipfile.ZipInfo.from_file(source, arcname=name)
    info.compress_type = zipfile.ZIP_DEFLATED
    with source.open("rb") as handle, archive.open(info, "w") as out:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
            out.write(chunk)
    return ManifestEntry(path=name, size_bytes=size, sha256=digest.hexdigest())


def create_backup(
    database: Database,
    layout: HistoryLayout,
    project_id: str,
    destination: Path,
    moment: datetime,
) -> BackupResult:
    """Publica o archive somente depois de escrito e verificado por completo."""
    target = resolve_destination(destination, layout, moment)
    staging = Path(tempfile.mkdtemp(prefix=".lastro-backup-", dir=target.parent))
    temporary = temporary_sibling(target)
    try:
        snapshot = staging / DATABASE_FILENAME
        _snapshot_database(database, snapshot)
        entries: list[ManifestEntry] = []
        with zipfile.ZipFile(temporary, "x") as archive:
            entries.append(_add_file(archive, snapshot, f"{project_id}/{DATABASE_FILENAME}"))
            for path, is_dir in _run_entries(layout.runs_dir):
                name = f"{project_id}/{path.relative_to(layout.root).as_posix()}"
                if is_dir:
                    archive.mkdir(name)
                else:
                    entries.append(_add_file(archive, path, name))
            manifest: dict[str, JsonValue] = {
                "format_version": BACKUP_FORMAT_VERSION,
                "project_id": project_id,
                "created_at": db_timestamp(moment),
                "lastro_version": get_version(),
                "schema_version": schema_version(database),
                "files": [
                    {"path": entry.path, "size_bytes": entry.size_bytes, "sha256": entry.sha256}
                    for entry in entries
                ],
            }
            archive.writestr(
                MANIFEST_FILENAME, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
            )
        with zipfile.ZipFile(temporary) as archive:
            broken = archive.testzip()
            if broken is not None:
                raise BackupError(f"o archive gerado está corrompido em {broken}")
        with temporary.open("rb+") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)
    return BackupResult(archive=target, project_id=project_id, files=tuple(entries))


def verify_restored_backup(folder: Path) -> list[str]:
    """Confere cada arquivo extraído contra o manifesto; retorna os problemas encontrados."""
    manifest_path = folder / MANIFEST_FILENAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupError(f"manifesto ilegível em {manifest_path}: {exc}") from None
    problems: list[str] = []
    for entry in manifest.get("files", []):
        relative = str(entry["path"])
        path = folder.joinpath(*relative.split("/"))
        if not path.is_file():
            problems.append(f"ausente: {relative}")
            continue
        size, sha256 = file_digest(path)
        if size != entry["size_bytes"] or sha256 != entry["sha256"]:
            problems.append(f"hash ou tamanho diferente: {relative}")
    for extra in (
        folder.joinpath(str(manifest.get("project_id", "")), DATABASE_FILENAME + suffix)
        for suffix in ("-wal", "-shm")
    ):
        if extra.exists():
            problems.append(f"arquivo inesperado: {extra.name}")
    return problems
