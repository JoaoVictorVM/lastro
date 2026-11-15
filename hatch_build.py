"""Hook de build: copia o catálogo canônico do demo para os recursos privados do avaliador.

O catálogo nunca é editado em duas cópias. O wheel recebe a cópia gerada aqui a partir de
`evals/catalog/demo-bugs.yaml`; o sdist leva o canônico e este hook para reconstruir o wheel.
O pacote do demo não recebe o arquivo.
"""

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

CANONICAL = Path("evals") / "catalog" / "demo-bugs.yaml"
PACKAGED = "lastro/evaluation/resources/catalog/demo-bugs.yaml"


class CatalogBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if self.target_name != "wheel":
            return
        source = Path(self.root) / CANONICAL
        if not source.is_file():
            raise FileNotFoundError(f"catálogo canônico ausente: {source}")
        build_data.setdefault("force_include", {})[str(source)] = PACKAGED
