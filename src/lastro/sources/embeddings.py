"""Modelo local de embeddings: preparo humano explícito, fingerprint e inferência em CPU.

Só `sources model prepare` baixa artefatos, sempre para o data root privado. Sync e consulta
carregam com `local_files_only`; modelo ausente ou alterado é erro acionável, nunca download
implícito nem fallback lexical silencioso.
"""

import hashlib
import importlib
import json
import math
import re
from array import array
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Protocol, cast

from lastro.knowledge.models import JsonObject
from lastro.sources.models import (
    DIMENSION,
    EMBEDDING_BATCH,
    INDEX_INCOMPATIBLE,
    MODEL_NAME,
    MODEL_NOT_READY,
    PASSAGE_PREFIX,
    QUERY_PREFIX,
    SOURCES_UNAVAILABLE,
    SourcesError,
)

MODELS_DIRNAME = "models"
MANIFEST_NAME = "lastro-model.json"
REQUIRED_MODULES = ("fastembed", "sqlite_vec", "pypdf", "markdown_it", "bs4", "tokenizers")
PACKAGES = ("fastembed", "onnxruntime", "tokenizers", "sqlite-vec")
_HASH_BLOCK = 1024 * 1024


def models_dir(private_data_root: Path) -> Path:
    """Cache do modelo fora da pasta do histórico: backups não carregam gigabytes de pesos."""
    return private_data_root / MODELS_DIRNAME / "fastembed"


def missing_modules() -> list[str]:
    missing: list[str] = []
    for name in REQUIRED_MODULES:
        try:
            importlib.import_module(name)
        except ImportError:
            missing.append(name)
    return missing


def require_extra() -> None:
    missing = missing_modules()
    if missing:
        raise SourcesError(
            SOURCES_UNAVAILABLE,
            "Extra sources não instalado (faltam: "
            + ", ".join(missing)
            + "). Rode uv sync --extra sources (ou pip install 'lastro[sources]').",
        )


class Tokenizer(Protocol):
    fingerprint: str

    def spans(self, text: str) -> list[tuple[int, int]]: ...


class EmbeddingModel(Protocol):
    fingerprint: str
    manifest: JsonObject
    tokenizer: Tokenizer

    def embed_passages(self, texts: Sequence[str]) -> list[bytes]: ...

    def embed_query(self, text: str) -> bytes: ...


def pack(values: Sequence[float]) -> bytes:
    """float32 little-endian, normalizado L2, com dimensão e finitude conferidas."""
    if len(values) != DIMENSION:
        raise SourcesError(INDEX_INCOMPATIBLE, "Vetor com dimensão inesperada.")
    norm = math.sqrt(sum(float(value) * float(value) for value in values))
    if not math.isfinite(norm) or norm == 0.0:
        raise SourcesError(INDEX_INCOMPATIBLE, "Vetor nulo ou não finito.")
    data = array("f", (float(value) / norm for value in values))
    if not all(math.isfinite(value) for value in data):
        raise SourcesError(INDEX_INCOMPATIBLE, "Vetor não finito.")
    return data.tobytes()


def unpack(blob: bytes) -> list[float]:
    data = array("f")
    data.frombytes(blob)
    return list(data)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(_HASH_BLOCK):
            digest.update(block)
    return digest.hexdigest()


def _versions() -> dict[str, str]:
    found: dict[str, str] = {}
    for name in PACKAGES:
        try:
            found[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            found[name] = "missing"
    return found


def _canonical(data: JsonObject) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _snapshot_dir(cache: Path) -> Path | None:
    for candidate in sorted(cache.glob("models--*/snapshots/*")):
        if (candidate / "tokenizer.json").is_file():
            return candidate
    for candidate in sorted(cache.rglob("tokenizer.json")):
        return candidate.parent
    return None


@dataclass(frozen=True)
class ModelStatus:
    ready: bool
    reason: str | None
    fingerprint: str | None
    detail: str


def read_manifest(cache: Path) -> JsonObject | None:
    path = cache / MANIFEST_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cast(JsonObject, data) if isinstance(data, dict) else None


def manifest_fingerprint(manifest: JsonObject) -> str:
    return hashlib.sha256(_canonical(manifest).encode("utf-8")).hexdigest()


def model_status(private_data_root: Path) -> ModelStatus:
    """Conferência barata (tamanhos e presença); o hash completo é feito no preparo."""
    missing = missing_modules()
    if missing:
        return ModelStatus(False, SOURCES_UNAVAILABLE, None, "extra sources não instalado")
    cache = models_dir(private_data_root)
    manifest = read_manifest(cache)
    if manifest is None:
        return ModelStatus(False, MODEL_NOT_READY, None, "modelo local não preparado")
    root = cache / str(manifest.get("directory", ""))
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        return ModelStatus(False, MODEL_NOT_READY, None, "manifest do modelo inválido")
    for name, info in files.items():
        size = info.get("size") if isinstance(info, dict) else None
        path = root / name
        if not path.is_file() or path.stat().st_size != size:
            return ModelStatus(False, MODEL_NOT_READY, None, "artefato do modelo ausente")
    if manifest.get("packages") != _versions():
        return ModelStatus(
            False, MODEL_NOT_READY, None, "pacotes mudaram desde o preparo do modelo"
        )
    fingerprint = manifest_fingerprint(manifest)
    return ModelStatus(True, None, fingerprint, f"{MODEL_NAME} preparado")


def prepare_model(private_data_root: Path, downloader: Callable[[Path], None] | None = None) -> str:
    """Download explícito e validação; falha preserva o manifest anterior."""
    require_extra()
    cache = models_dir(private_data_root)
    cache.mkdir(parents=True, exist_ok=True)
    (downloader or _download)(cache)
    snapshot = _snapshot_dir(cache)
    if snapshot is None:
        raise SourcesError(MODEL_NOT_READY, "Download do modelo incompleto: tokenizer ausente.")
    files: JsonObject = {}
    for path in sorted(snapshot.iterdir()):
        if path.is_file():
            files[path.name] = {"sha256": _file_sha256(path), "size": path.stat().st_size}
    manifest: JsonObject = {
        "model": MODEL_NAME,
        "directory": snapshot.relative_to(cache).as_posix(),
        "revision": snapshot.name,
        "files": files,
        "packages": cast(JsonObject, _versions()),
        "dimension": DIMENSION,
        "prefixes": {"query": QUERY_PREFIX, "passage": PASSAGE_PREFIX},
        "normalization": "l2",
        "device": "cpu",
        "batch": EMBEDDING_BATCH,
    }
    target = cache / MANIFEST_NAME
    temporary = target.with_suffix(".tmp")
    temporary.write_text(_canonical(manifest), encoding="utf-8")
    temporary.replace(target)
    return manifest_fingerprint(manifest)


def _download(cache: Path) -> None:
    fastembed: Any = importlib.import_module("fastembed")
    try:
        fastembed.TextEmbedding(model_name=MODEL_NAME, cache_dir=str(cache))
    except Exception as exc:
        raise SourcesError(
            MODEL_NOT_READY, f"Falha ao preparar o modelo: {type(exc).__name__}"
        ) from None


class HuggingFaceTokenizer:
    def __init__(self, path: Path, fingerprint: str) -> None:
        tokenizers: Any = importlib.import_module("tokenizers")
        self._tokenizer: Any = tokenizers.Tokenizer.from_file(str(path))
        self.fingerprint = fingerprint

    def spans(self, text: str) -> list[tuple[int, int]]:
        encoding = self._tokenizer.encode(text, add_special_tokens=False)
        result: list[tuple[int, int]] = []
        for start, end in cast(list[tuple[int, int]], encoding.offsets):
            if end > start:
                result.append((int(start), int(end)))
        return result


class FastEmbedModel:
    """Modelo carregado só do cache local; qualquer divergência do manifest recusa o uso."""

    def __init__(self, private_data_root: Path) -> None:
        status = model_status(private_data_root)
        if not status.ready or status.fingerprint is None:
            raise SourcesError(
                status.reason or MODEL_NOT_READY,
                f"{status.detail}. Rode 'lastro sources model prepare' antes de sincronizar "
                "ou consultar.",
            )
        cache = models_dir(private_data_root)
        manifest = read_manifest(cache)
        assert manifest is not None
        self.manifest: JsonObject = manifest
        self.fingerprint = status.fingerprint
        directory = cache / str(manifest["directory"])
        files = cast(dict[str, Any], manifest["files"])
        tokenizer_hash = str(files["tokenizer.json"]["sha256"])
        self.tokenizer: Tokenizer = HuggingFaceTokenizer(
            directory / "tokenizer.json", tokenizer_hash
        )
        fastembed: Any = importlib.import_module("fastembed")
        try:
            self._model: Any = fastembed.TextEmbedding(
                model_name=MODEL_NAME, cache_dir=str(cache), local_files_only=True
            )
        except Exception:
            raise SourcesError(
                MODEL_NOT_READY,
                "Modelo local indisponível. Rode 'lastro sources model prepare'.",
            ) from None

    def _embed(self, texts: Sequence[str]) -> list[bytes]:
        vectors = self._model.embed(list(texts), batch_size=EMBEDDING_BATCH)
        return [pack([float(value) for value in vector]) for vector in vectors]

    def embed_passages(self, texts: Sequence[str]) -> list[bytes]:
        return self._embed([PASSAGE_PREFIX + text for text in texts])

    def embed_query(self, text: str) -> bytes:
        return self._embed([QUERY_PREFIX + text])[0]


type ModelFactory = Callable[[], EmbeddingModel]


# Double determinístico para testes e smoke sem modelo: nunca usado como evidência de qualidade.
_WORD = re.compile(r"\w+", re.UNICODE)


class WordTokenizer:
    fingerprint = hashlib.sha256(b"lastro-word-tokenizer-v1").hexdigest()

    def spans(self, text: str) -> list[tuple[int, int]]:
        return [match.span() for match in _WORD.finditer(text)]


class HashingModel:
    """Bag of words com hashing em 1.024 dimensões, normalizado; só para testes."""

    def __init__(self, salt: str = "v1") -> None:
        self.tokenizer: Tokenizer = WordTokenizer()
        self.manifest: JsonObject = {"model": "hashing-test-double", "salt": salt}
        self.fingerprint = manifest_fingerprint(self.manifest)
        self.calls = 0

    def _vector(self, text: str) -> bytes:
        values = [0.0] * DIMENSION
        for word in _WORD.findall(text.lower()):
            digest = hashlib.sha256(word.encode("utf-8")).digest()
            values[int.from_bytes(digest[:4], "little") % DIMENSION] += 1.0
        if not any(values):
            values[0] = 1.0
        return pack(values)

    def embed_passages(self, texts: Sequence[str]) -> list[bytes]:
        self.calls += len(texts)
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> bytes:
        return self._vector(text)
