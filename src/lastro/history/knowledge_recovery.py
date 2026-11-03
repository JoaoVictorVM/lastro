"""Fallback privado do resultado de memória quando a transação terminal não grava no banco.

O arquivo é o registro provisório de `persistence_failed`, não uma segunda conclusão. A
reconciliação valida projeto, run, schema e hashes antes de importar de forma idempotente.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from lastro.history.layout import write_bytes_atomically
from lastro.history.models import JsonObject, JsonValue
from lastro.history.redaction import canonical_json, redact_text, sha256_hex

FALLBACK_NAME = "knowledge_pending.json"
IMPORTED_SUFFIX = ".imported"
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class MemoryFallback:
    path: Path
    run_id: str
    project_id: str
    proposals: list[JsonObject]
    file_sha256: str

    @property
    def attempt_key(self) -> str:
        return "fallback:" + self.file_sha256


def fallback_path(run_dir: Path) -> Path:
    return run_dir / FALLBACK_NAME


def export_fallback(
    run_dir: Path,
    *,
    project_id: str,
    run_id: str,
    proposals: list[JsonObject],
    error: str,
    timestamp: str,
) -> Path:
    document: JsonObject = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "run_id": run_id,
        "outcome": "persistence_failed",
        "error": redact_text(error),
        "created_at": timestamp,
        "proposals": cast(list[JsonValue], proposals),
    }
    target = fallback_path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    write_bytes_atomically(target, (canonical_json(document) + "\n").encode("utf-8"))
    return target


class FallbackError(ValueError):
    pass


def _proposal(item: JsonValue) -> JsonObject:
    if not isinstance(item, dict):
        raise FallbackError("proposal must be an object")
    for key in ("id", "idempotency_key", "content_sha256"):
        if not isinstance(item.get(key), str):
            raise FallbackError(f"proposal without {key}")
    payload = item.get("payload")
    if not isinstance(payload, dict):
        raise FallbackError("proposal without payload")
    if sha256_hex(canonical_json(payload)) != item["content_sha256"]:
        raise FallbackError("proposal hash differs")
    return item


def load_fallback(path: Path, project_id: str) -> MemoryFallback:
    raw = path.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise FallbackError(f"invalid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise FallbackError("document must be an object")
    document = cast(JsonObject, data)
    if document.get("schema_version") != SCHEMA_VERSION:
        raise FallbackError("unknown schema_version")
    if document.get("project_id") != project_id:
        raise FallbackError("fallback belongs to another project")
    run_id = document.get("run_id")
    if not isinstance(run_id, str) or path.parent.name != run_id:
        raise FallbackError("run_id differs from the run folder")
    proposals = document.get("proposals")
    if not isinstance(proposals, list):
        raise FallbackError("proposals must be a list")
    return MemoryFallback(
        path=path,
        run_id=run_id,
        project_id=project_id,
        proposals=[_proposal(item) for item in proposals],
        file_sha256=sha256_hex(raw),
    )


def find_fallbacks(runs_dir: Path) -> list[Path]:
    if not runs_dir.is_dir():
        return []
    return sorted(path for path in runs_dir.glob(f"*/{FALLBACK_NAME}") if path.is_file())


def mark_imported(path: Path) -> None:
    """Renomeia em vez de apagar: o arquivo continua como proveniência da recuperação."""
    os.replace(path, path.with_name(path.name + IMPORTED_SUFFIX))
