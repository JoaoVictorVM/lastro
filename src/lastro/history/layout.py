"""Layout da pasta privada do projeto e escrita atômica de arquivos."""

import contextlib
import os
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

DATABASE_FILENAME = "history.sqlite"
ACTIVE_RUN_FILENAME = "active-run.json"
RUNS_DIRNAME = "runs"
EVIDENCE_DIRNAME = "evidence"
MISSION_SNAPSHOT_RELATIVE = "mission/source.md"
FALLBACK_FILENAME = "fallback-events.jsonl"


@dataclass(frozen=True)
class HistoryLayout:
    root: Path

    @property
    def database(self) -> Path:
        return self.root / DATABASE_FILENAME

    @property
    def active_run(self) -> Path:
        return self.root / ACTIVE_RUN_FILENAME

    @property
    def runs_dir(self) -> Path:
        return self.root / RUNS_DIRNAME

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def evidence_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / EVIDENCE_DIRNAME

    def mission_snapshot(self, run_id: str) -> Path:
        return self.run_dir(run_id).joinpath(*MISSION_SNAPSHOT_RELATIVE.split("/"))

    def fallback(self, run_id: str) -> Path:
        return self.run_dir(run_id) / FALLBACK_FILENAME


def temporary_sibling(target: Path) -> Path:
    # Temporários ficam na pasta do destino para que a troca final seja atômica.
    return target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")


def write_chunks_atomically(target: Path, chunks: Iterable[bytes]) -> None:
    """Grava em temporário, sincroniza e troca; uma falha remove o temporário."""
    temporary = temporary_sibling(target)
    try:
        with temporary.open("xb") as handle:
            for chunk in chunks:
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)


def write_bytes_atomically(target: Path, data: bytes) -> None:
    write_chunks_atomically(target, (data,))
