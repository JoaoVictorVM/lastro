"""Bundle canônico de skills: arquivos Markdown do pacote, manifest e hash de conteúdo."""

import hashlib
import json
from collections.abc import Mapping
from importlib.resources import files

SCHEMA_VERSION = 1
SKILL_FILES = ("explore.md", "qa-heuristics.md", "reproduce.md")
MANIFEST_FILE = "manifest.json"


def normalize(content: bytes) -> bytes:
    # O checkout com autocrlf troca as quebras de linha; o hash precisa ser o mesmo em Windows e
    # Linux para que todos os executores compartilhem a identidade do bundle.
    return content.replace(b"\r\n", b"\n")


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def canonical_files() -> dict[str, bytes]:
    package = files("lastro.skills")
    return {name: normalize(package.joinpath(name).read_bytes()) for name in SKILL_FILES}


def bundle_hash(contents: Mapping[str, bytes]) -> str:
    """SHA-256 do envelope JSON com paths POSIX ordenados e o hash de cada Markdown."""
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "files": [
            {"path": name, "sha256": sha256_hex(normalize(contents[name]))}
            for name in sorted(contents)
        ],
    }
    canonical = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256_hex(canonical.encode("utf-8"))


def manifest(contents: Mapping[str, bytes]) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "bundle_sha256": bundle_hash(contents),
        "files": [
            {
                "path": name,
                "size_bytes": len(normalize(contents[name])),
                "sha256": sha256_hex(normalize(contents[name])),
            }
            for name in sorted(contents)
        ],
    }


def manifest_text(contents: Mapping[str, bytes]) -> str:
    return json.dumps(manifest(contents), indent=2, ensure_ascii=False) + "\n"
