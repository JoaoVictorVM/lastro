"""Carregamento seguro das suites YAML, resolução de recursos e expansão reproduzível dos slots."""

import hashlib
import json
import random
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from lastro.evaluation.models import (
    MAX_MISSION_CHARS,
    MAX_SUITE_BYTES,
    SAFE_ID,
    Condition,
    EvalError,
    MissionSpec,
    ScenarioSpec,
    SuiteSpec,
    TrialRole,
    TrialSlot,
)

RESOURCE_PACKAGE = "lastro.evaluation.resources"
SUITES_DIR = "suites"
RUBRIC_FILE = "judge-rubric.md"
CATALOG_RELATIVE = ("catalog", "demo-bugs.yaml")
CANONICAL_CATALOG = Path("evals") / "catalog" / "demo-bugs.yaml"
INSTALL_HINT = "instale o extra de avaliação: uv sync --extra eval (ou pip install 'lastro[eval]')"
# Cenários cuja observação depende de gates, verificação de tenant ou memória do Lastro: a
# baseline não tem esses componentes e o slot vira not_applicable, nunca um sucesso artificial.
LASTRO_ONLY = {"gates", "tenant", "memory"}


def require_yaml() -> Any:
    try:
        import yaml
    except ImportError:
        raise EvalError("eval_extra_missing", INSTALL_HINT) from None
    return yaml


def _strict_loader(yaml: Any) -> Any:
    """SafeLoader sem aliases, tags explícitas nem chaves duplicadas."""

    class StrictLoader(yaml.SafeLoader):
        def compose_node(self, parent: Any, index: Any) -> Any:
            if self.check_event(yaml.AliasEvent):
                raise EvalError("invalid_suite", "aliases/anchors não são aceitos")
            event = self.peek_event()
            if getattr(event, "anchor", None):
                raise EvalError("invalid_suite", "aliases/anchors não são aceitos")
            if getattr(event, "tag", None) is not None:
                raise EvalError("invalid_suite", "tags explícitas não são aceitas")
            return cast(Any, super().compose_node(parent, index))  # pyright: ignore[reportUnknownMemberType]

        def construct_mapping(self, node: Any, deep: bool = False) -> Any:
            seen: set[Any] = set()
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=deep)
                if key in seen:
                    raise EvalError("invalid_suite", f"chave duplicada: {key}")
                seen.add(key)
            return cast(Any, super().construct_mapping(node, deep=deep))  # pyright: ignore[reportUnknownMemberType]

    return StrictLoader


def parse_yaml(raw: bytes) -> Any:
    yaml = require_yaml()
    if len(raw) > MAX_SUITE_BYTES:
        raise EvalError("invalid_suite", "arquivo acima de 1 MiB")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise EvalError("invalid_suite", "arquivo não é UTF-8") from None
    loader = _strict_loader(yaml)
    try:
        documents = list(yaml.load_all(text, Loader=loader))
    except EvalError:
        raise
    except yaml.YAMLError as exc:
        raise EvalError("invalid_suite", f"YAML inválido: {exc}") from None
    if len(documents) != 1:
        raise EvalError("invalid_suite", "a suite deve ter exatamente um documento")
    return documents[0]


@dataclass(frozen=True)
class SuiteRoot:
    """Raiz de resolução: recursos do pacote ou a pasta da suite local."""

    packaged: bool
    directory: Path | None = None

    def _traversable(self) -> Traversable:
        return resources.files(RESOURCE_PACKAGE)

    def read_bytes(self, relative: str) -> bytes:
        parts = _safe_parts(relative)
        if self.packaged:
            node = self._traversable()
            for part in parts:
                node = node.joinpath(part)
            if not node.is_file():
                raise EvalError("invalid_suite", f"recurso ausente: {relative}")
            return node.read_bytes()
        assert self.directory is not None
        root = self.directory.resolve()
        target = root.joinpath(*parts)
        resolved = target.resolve()
        if resolved != root and root not in resolved.parents:
            raise EvalError("invalid_suite", f"caminho fora da raiz da suite: {relative}")
        if target.is_symlink():
            raise EvalError("invalid_suite", f"symlink não aceito: {relative}")
        try:
            return resolved.read_bytes()
        except OSError:
            raise EvalError("invalid_suite", f"arquivo ausente: {relative}") from None


def _safe_parts(relative: str) -> tuple[str, ...]:
    normalized = relative.replace("\\", "/")
    if normalized.startswith("/") or (len(normalized) > 1 and normalized[1] == ":"):
        raise EvalError("invalid_suite", f"caminho absoluto não aceito: {relative}")
    parts = tuple(part for part in normalized.split("/") if part not in ("", "."))
    if not parts or any(part == ".." for part in parts):
        raise EvalError("invalid_suite", f"caminho inválido: {relative}")
    return parts


@dataclass(frozen=True)
class LoadedSuite:
    spec: SuiteSpec
    root: SuiteRoot
    raw_sha256: str
    missions: dict[str, str]
    source: str

    def mission_text(self, mission_id: str) -> str:
        return self.missions[mission_id]

    def scenario(self, scenario_id: str) -> ScenarioSpec:
        return next(item for item in self.spec.scenarios if item.id == scenario_id)


def validate_suite(data: Any) -> SuiteSpec:
    if not isinstance(data, dict):
        raise EvalError("invalid_suite", "a suite deve ser um mapeamento")
    try:
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise EvalError("invalid_suite", "valores não representáveis em JSON") from None
    try:
        return SuiteSpec.model_validate_json(encoded)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"]) or "suite"
        raise EvalError("invalid_suite", f"{location}: {first['msg']}") from None


def _load_missions(spec: SuiteSpec, root: SuiteRoot) -> dict[str, str]:
    texts: dict[str, str] = {}
    for mission in spec.missions:
        try:
            text = root.read_bytes(mission.file).decode("utf-8").replace("\r\n", "\n")
        except UnicodeDecodeError:
            raise EvalError("invalid_suite", f"missão {mission.id} não é UTF-8") from None
        if not text.strip() or len(text) > MAX_MISSION_CHARS:
            raise EvalError("invalid_suite", f"missão {mission.id} vazia ou acima do limite")
        texts[mission.id] = text
    return texts


def packaged_suite_names() -> list[str]:
    folder = resources.files(RESOURCE_PACKAGE).joinpath(SUITES_DIR)
    return sorted(
        item.name.removesuffix(".yaml") for item in folder.iterdir() if item.name.endswith(".yaml")
    )


def load_suite(reference: str, *, cwd: Path | None = None) -> LoadedSuite:
    """Nome distribuído (`demo-core`) ou caminho de YAML local."""
    path = Path(reference)
    looks_like_path = reference.endswith((".yaml", ".yml")) or "/" in reference or "\\" in reference
    if looks_like_path:
        if not path.is_absolute() and cwd is not None:
            path = cwd / path
        try:
            raw = path.read_bytes()
        except OSError:
            raise EvalError("invalid_suite", f"suite não encontrada: {reference}") from None
        root = SuiteRoot(packaged=False, directory=path.parent)
        source = str(path)
    else:
        if not SAFE_ID.fullmatch(reference):
            raise EvalError("invalid_suite", f"nome de suite inválido: {reference}")
        root = SuiteRoot(packaged=True)
        try:
            raw = root.read_bytes(f"{SUITES_DIR}/{reference}.yaml")
        except EvalError:
            known = ", ".join(packaged_suite_names())
            raise EvalError(
                "invalid_suite", f"suite desconhecida: {reference}. Disponíveis: {known}"
            ) from None
        source = f"package:{reference}"
    spec = validate_suite(parse_yaml(raw))
    missions = _load_missions(spec, root)
    return LoadedSuite(
        spec=spec,
        root=root,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        missions=missions,
        source=source,
    )


def scenario_applies(scenario: ScenarioSpec, condition: Condition) -> bool:
    if scenario.conditions is not None and condition not in scenario.conditions:
        return False
    if condition == Condition.BASELINE and LASTRO_ONLY.intersection(scenario.requires):
        return False
    return not (condition == Condition.NO_MEMORY and "memory" in scenario.requires)


def block_seed(seed: int, repetition: int) -> int:
    """Seed do demo por bloco: igual entre condições do bloco, diferente entre blocos."""
    digest = hashlib.sha256(f"{seed}:{repetition}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % 2_147_483_647


def expand_slots(spec: SuiteSpec) -> list[TrialSlot]:
    """Bloco por repetição; dentro de cada bloco, condições embaralhadas pelo seed persistido.

    Com `pairing=warmup`, cada bloco abre com um aquecimento `full` que antecede todos os trials
    medidos do bloco; o aquecimento nunca entra no N.
    """
    rng = random.Random(spec.seed)
    slots: list[TrialSlot] = []
    ordinal = 0
    for repetition in range(1, spec.runs_per_condition + 1):
        demo_seed = block_seed(spec.seed, repetition)
        for scenario in spec.scenarios:
            warmup_ordinal: int | None = None
            if spec.pairing == "warmup":
                ordinal += 1
                warmup_ordinal = ordinal
                slots.append(
                    TrialSlot(
                        ordinal=ordinal,
                        condition=Condition.FULL,
                        scenario=scenario.id,
                        variant=scenario.demo.variant,
                        repetition=repetition,
                        role=TrialRole.WARMUP,
                        applicable=True,
                        demo_seed=demo_seed,
                    )
                )
            conditions = list(spec.conditions)
            rng.shuffle(conditions)
            for condition in conditions:
                ordinal += 1
                slots.append(
                    TrialSlot(
                        ordinal=ordinal,
                        condition=condition,
                        scenario=scenario.id,
                        variant=scenario.demo.variant,
                        repetition=repetition,
                        role=TrialRole.MEASURED,
                        applicable=scenario_applies(scenario, condition),
                        parent_ordinal=warmup_ordinal,
                        demo_seed=demo_seed,
                    )
                )
    return slots


def mission_for(spec: SuiteSpec, scenario_id: str) -> MissionSpec:
    scenario = next(item for item in spec.scenarios if item.id == scenario_id)
    return spec.mission(scenario.mission)


def canonical_catalog_path(start: Path | None = None) -> Path | None:
    """Catálogo canônico do checkout; no wheel instalado, ausente."""
    here = (start or Path(__file__)).resolve()
    for candidate in here.parents:
        path = candidate / CANONICAL_CATALOG
        if path.is_file() and (candidate / "pyproject.toml").is_file():
            return path
    return None


def catalog_bytes() -> bytes:
    """Gabarito do avaliador: no checkout o canônico; instalado, a cópia gerada no build."""
    canonical = canonical_catalog_path()
    if canonical is not None:
        return canonical.read_bytes()
    node = resources.files(RESOURCE_PACKAGE)
    for part in CATALOG_RELATIVE:
        node = node.joinpath(part)
    if not node.is_file():
        raise EvalError(
            "eval_extra_missing", "catálogo do avaliador ausente na instalação; " + INSTALL_HINT
        )
    return node.read_bytes()


def rubric_text() -> str:
    return SuiteRoot(packaged=True).read_bytes(RUBRIC_FILE).decode("utf-8").replace("\r\n", "\n")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def manifest(loaded: LoadedSuite, slots: list[TrialSlot], executor: str) -> dict[str, Any]:
    """Snapshot imutável da suite: tudo o que o resume precisa sem reler o YAML."""
    spec = loaded.spec.model_copy(update={"executor": executor})
    return {
        "schema_version": 1,
        "source": loaded.source,
        "suite": json.loads(spec.model_dump_json()),
        "missions": {key: sha256_text(text) for key, text in loaded.missions.items()},
        "mission_texts": loaded.missions,
        "slots": [
            {
                "ordinal": slot.ordinal,
                "condition": slot.condition.value,
                "scenario": slot.scenario,
                "variant": slot.variant,
                "repetition": slot.repetition,
                "role": slot.role.value,
                "applicable": slot.applicable,
                "parent_ordinal": slot.parent_ordinal,
                "demo_seed": slot.demo_seed,
            }
            for slot in slots
        ],
    }


def slots_from_manifest(data: dict[str, Any]) -> list[TrialSlot]:
    return [
        TrialSlot(
            ordinal=int(item["ordinal"]),
            condition=Condition(item["condition"]),
            scenario=str(item["scenario"]),
            variant=str(item["variant"]),
            repetition=int(item["repetition"]),
            role=TrialRole(item["role"]),
            applicable=bool(item["applicable"]),
            parent_ordinal=item["parent_ordinal"],
            demo_seed=int(item["demo_seed"]),
        )
        for item in data["slots"]
    ]


def spec_from_manifest(data: dict[str, Any]) -> SuiteSpec:
    return SuiteSpec.model_validate_json(json.dumps(data["suite"]))


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
