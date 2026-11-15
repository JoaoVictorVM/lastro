"""Condições efetivas da avaliação, bundles derivados e o vínculo confiável pela run.

O supervisor grava o bloco `evaluation` no snapshot da run antes do início; tools, grafo nativo e
reprodução leem a condição só dali. Nenhum argumento de tool ou conteúdo de página liga ou
desliga componentes.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lastro.evaluation.models import Condition, EvalError
from lastro.skills import SKILL_FILES, bundle_hash, canonical_files, normalize, sha256_hex

EVALUATION_BLOCK = "evaluation"
DERIVED_MANIFEST = "eval-manifest.json"
DERIVED_KIND = "lastro-eval-derived-bundle"
DERIVED_SCHEMA = 1
MEMORY_TOOLS = frozenset({"knowledge_query", "knowledge_propose", "knowledge_revalidate"})
ORACLES_NOTICE = (
    "\n\n## Evaluation notice\n\nAutomatic oracles are unavailable in this run: observe returns "
    "no oracle signals. Rely on what you observe directly.\n"
)
# Marcadores das linhas removidas em cada ablação; a remoção é por linha para que regras de
# segurança, validação estrutural e evidência obrigatória continuem intactas.
HEURISTIC_MARKERS = ("qa-heuristics", "heuristic")
MEMORY_MARKERS = ("knowledge_", "memory", "remembered")
REPRODUCTION_MARKERS = ("reproduce.md", "reproduction", "report_reproduction_result")


@dataclass(frozen=True)
class EffectiveCondition:
    condition: Condition
    heuristics: bool
    oracles: bool
    reproduction: bool
    memory: bool
    lastro: bool

    @classmethod
    def of(cls, condition: Condition) -> "EffectiveCondition":
        if condition == Condition.BASELINE:
            return cls(condition, False, False, False, False, False)
        return cls(
            condition=condition,
            heuristics=condition != Condition.NO_HEURISTICS,
            oracles=condition != Condition.NO_ORACLES,
            reproduction=condition != Condition.NO_REPRODUCTION,
            memory=condition != Condition.NO_MEMORY,
            lastro=True,
        )

    def components(self) -> dict[str, bool]:
        return {
            "heuristics": self.heuristics,
            "oracles": self.oracles,
            "reproduction": self.reproduction,
            "memory": self.memory,
            "lastro": self.lastro,
        }

    @property
    def sha256(self) -> str:
        payload = {"condition": self.condition.value, **self.components()}
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @property
    def disabled_tools(self) -> frozenset[str]:
        return frozenset() if self.memory else MEMORY_TOOLS


def binding_block(
    effective: EffectiveCondition, *, suite_id: str, trial_id: str, bundle_sha256: str | None
) -> dict[str, Any]:
    """Bloco gravado pelo supervisor no snapshot da run; o hash amarra todos os campos."""
    body = {
        "suite_id": suite_id,
        "trial_id": trial_id,
        "condition": effective.condition.value,
        "components": effective.components(),
        "bundle_sha256": bundle_sha256,
    }
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {**body, "binding_sha256": digest}


def from_snapshot(snapshot: Mapping[str, Any]) -> EffectiveCondition | None:
    """Condição efetiva da run; bloco adulterado é rejeitado em vez de virar full silencioso."""
    block = snapshot.get(EVALUATION_BLOCK)
    if block is None:
        return None
    if not isinstance(block, dict):
        raise EvalError("isolation_unavailable", "evaluation block is malformed")
    data: dict[str, Any] = dict(block)  # pyright: ignore[reportUnknownArgumentType]
    expected = data.pop("binding_sha256", None)
    digest = hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if expected != digest:
        raise EvalError("isolation_unavailable", "evaluation binding does not match")
    try:
        effective = EffectiveCondition.of(Condition(str(data["condition"])))
    except (KeyError, ValueError):
        raise EvalError("isolation_unavailable", "unknown evaluation condition") from None
    if data.get("components") != effective.components():
        raise EvalError("isolation_unavailable", "evaluation components were changed")
    return effective


def child_snapshot_block(parent: Mapping[str, Any]) -> dict[str, Any]:
    """Reproduções herdam exatamente o bloco da exploração que as originou."""
    block = parent.get(EVALUATION_BLOCK)
    return {EVALUATION_BLOCK: block} if isinstance(block, dict) else {}


def _strip_lines(text: str, markers: tuple[str, ...]) -> str:
    kept = [
        line for line in text.split("\n") if not any(marker in line.lower() for marker in markers)
    ]
    return "\n".join(kept)


@dataclass(frozen=True)
class DerivedBundle:
    files: dict[str, bytes]
    manifest: dict[str, Any]

    @property
    def sha256(self) -> str:
        return str(self.manifest["bundle_sha256"])

    def text(self) -> str:
        sections = [
            f'<skill-file path="skills/lastro/{name}">\n'
            + self.files[name].decode("utf-8")
            + "\n</skill-file>"
            for name in sorted(self.files)
        ]
        return "\n\n".join(sections)


def derive_bundle(
    effective: EffectiveCondition, base: Mapping[str, bytes] | None = None
) -> DerivedBundle | None:
    """Bundle efetivo da condição; baseline não recebe bundle algum."""
    if not effective.lastro:
        return None
    contents = {name: normalize(data) for name, data in (base or canonical_files()).items()}
    base_sha = bundle_hash(contents)
    transformations: list[str] = []
    files = dict(contents)
    if not effective.heuristics:
        files.pop("qa-heuristics.md", None)
        transformations.append("remove:qa-heuristics.md")
        for name in list(files):
            files[name] = _strip_lines(files[name].decode("utf-8"), HEURISTIC_MARKERS).encode()
        transformations.append("strip-lines:heuristics")
    if not effective.oracles:
        files["explore.md"] = files["explore.md"] + ORACLES_NOTICE.encode("utf-8")
        transformations.append("append:oracles-unavailable")
    if not effective.reproduction:
        files.pop("reproduce.md", None)
        transformations.append("remove:reproduce.md")
        for name in list(files):
            files[name] = _strip_lines(files[name].decode("utf-8"), REPRODUCTION_MARKERS).encode()
        transformations.append("strip-lines:reproduction")
    if not effective.memory:
        for name in list(files):
            files[name] = _strip_lines(files[name].decode("utf-8"), MEMORY_MARKERS).encode()
        transformations.append("strip-lines:memory")
    manifest: dict[str, Any] = {
        "kind": DERIVED_KIND,
        "schema_version": DERIVED_SCHEMA,
        "condition": effective.condition.value,
        "base_bundle_sha256": base_sha,
        "transformations": transformations,
        "files": [{"path": name, "sha256": sha256_hex(files[name])} for name in sorted(files)],
        "bundle_sha256": bundle_hash(files),
    }
    return DerivedBundle(files=files, manifest=manifest)


def write_derived_bundle(directory: Path, bundle: DerivedBundle) -> None:
    """Materializa no workspace da tentativa; o manifest normal nunca é escrito."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in SKILL_FILES:
        (directory / name).unlink(missing_ok=True)
    (directory / "manifest.json").unlink(missing_ok=True)
    for name, data in bundle.files.items():
        (directory / name).write_bytes(data)
    (directory / DERIVED_MANIFEST).write_text(
        json.dumps(bundle.manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def verify_derived_bundle(
    directory: Path, *, expected_condition: Condition, expected_base: str | None = None
) -> DerivedBundle:
    """Validação do harness: esquema próprio, hash do base, transformações e hash final."""
    try:
        manifest: dict[str, Any] = json.loads((directory / DERIVED_MANIFEST).read_bytes())
    except (OSError, ValueError):
        raise EvalError("isolation_unavailable", "derived bundle manifest missing") from None
    if manifest.get("kind") != DERIVED_KIND or manifest.get("schema_version") != DERIVED_SCHEMA:
        raise EvalError("isolation_unavailable", "derived bundle manifest has another schema")
    if manifest.get("condition") != expected_condition.value:
        raise EvalError("isolation_unavailable", "derived bundle belongs to another condition")
    rebuilt = derive_bundle(EffectiveCondition.of(expected_condition))
    assert rebuilt is not None
    if expected_base is not None and manifest.get("base_bundle_sha256") != expected_base:
        raise EvalError("isolation_unavailable", "derived bundle has another base")
    if manifest.get("transformations") != rebuilt.manifest["transformations"]:
        raise EvalError("isolation_unavailable", "derived bundle transformations differ")
    files: dict[str, bytes] = {}
    for entry in manifest.get("files", []):
        name = str(entry["path"])
        try:
            data = normalize((directory / name).read_bytes())
        except OSError:
            raise EvalError(
                "isolation_unavailable", f"derived bundle file {name} missing"
            ) from None
        if sha256_hex(data) != entry["sha256"]:
            raise EvalError("isolation_unavailable", f"derived bundle file {name} changed")
        files[name] = data
    if bundle_hash(files) != manifest.get("bundle_sha256"):
        raise EvalError("isolation_unavailable", "derived bundle hash differs from its content")
    return DerivedBundle(files=files, manifest=manifest)
