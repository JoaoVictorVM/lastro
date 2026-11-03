"""Bundle local em skills/lastro: materialização, validação e identidade por hash."""

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from lastro.skills import (
    MANIFEST_FILE,
    SCHEMA_VERSION,
    SKILL_FILES,
    bundle_hash,
    canonical_files,
    manifest_text,
    normalize,
    sha256_hex,
)

BUNDLE_DIR = Path("skills") / "lastro"
INSTRUCTION_MAX_LINES = 60
FIX = "rode 'lastro init' para materializar o bundle e as instruções do executor"
# Hashes de versões publicadas anteriormente; arquivos idênticos a elas são atualizados pelo init.
PREVIOUS_SHA256: dict[str, frozenset[str]] = {
    **{name: frozenset[str]() for name in SKILL_FILES},
    "explore.md": frozenset(
        {
            "2868cd7a1aca4952ebbdce1a09835f95ed9c7e90b562a1329c513cd3d87f6ab1",
            "0f7f787c3662b32c18518dbe423cc0cd075131b20995d2759a06fdb116d114fd",
        }
    ),
}


class ManifestEntry(BaseModel):
    path: str
    sha256: str


class Manifest(BaseModel):
    schema_version: int
    files: list[ManifestEntry]


class BundleError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(f"Bundle de skills inválido: {detail}; {FIX}.")
        self.detail = detail


@dataclass(frozen=True)
class LocalBundle:
    directory: Path
    sha256: str


def bundle_files() -> dict[Path, str]:
    """Arquivos gerenciados do bundle, relativos à raiz do projeto, com quebras LF."""
    contents = canonical_files()
    rendered = {BUNDLE_DIR / name: content.decode("utf-8") for name, content in contents.items()}
    rendered[BUNDLE_DIR / MANIFEST_FILE] = manifest_text(contents)
    return rendered


def instruction_references() -> tuple[str, ...]:
    return tuple((BUNDLE_DIR / name).as_posix() for name in SKILL_FILES)


def verify_bundle(project_dir: Path) -> LocalBundle:
    """Valida manifest e conteúdo do bundle local e devolve o hash efetivamente usado."""
    directory = project_dir / BUNDLE_DIR
    manifest_path = directory / MANIFEST_FILE
    try:
        manifest = Manifest.model_validate_json(manifest_path.read_bytes())
    except FileNotFoundError:
        raise BundleError(f"{(BUNDLE_DIR / MANIFEST_FILE).as_posix()} ausente") from None
    except (OSError, ValueError):
        raise BundleError(f"{(BUNDLE_DIR / MANIFEST_FILE).as_posix()} ilegível") from None
    if manifest.schema_version != SCHEMA_VERSION:
        raise BundleError("versão do manifest incompatível")
    expected = {entry.path: entry.sha256 for entry in manifest.files}
    if len(expected) != len(manifest.files):
        raise BundleError("o manifest repete arquivos")
    if sorted(expected) != sorted(SKILL_FILES):
        raise BundleError("o manifest deve listar exatamente " + ", ".join(SKILL_FILES))
    contents: dict[str, bytes] = {}
    for name in SKILL_FILES:
        try:
            content = normalize((directory / name).read_bytes())
        except OSError:
            raise BundleError(f"{(BUNDLE_DIR / name).as_posix()} ausente") from None
        if sha256_hex(content) != expected[name]:
            raise BundleError(f"{(BUNDLE_DIR / name).as_posix()} difere do manifest")
        contents[name] = content
    return LocalBundle(directory=directory, sha256=bundle_hash(contents))


def instruction_problems(path: Path, *, check_length: bool = True) -> list[str]:
    """Problemas do arquivo de instruções do executor; lista vazia quando está adequado."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return [f"{path.name} ausente"]
    problems: list[str] = []
    missing = [reference for reference in instruction_references() if reference not in text]
    if missing:
        problems.append(f"{path.name} não referencia " + ", ".join(missing))
    if check_length and len(text.splitlines()) > INSTRUCTION_MAX_LINES:
        problems.append(f"{path.name} excede {INSTRUCTION_MAX_LINES} linhas")
    return problems
