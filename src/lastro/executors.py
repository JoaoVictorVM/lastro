"""Executores suportados: binário procurado pelo doctor e arquivo de permissão gerado pelo init."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ExecutorSpec:
    name: str
    binary: str
    permission_file: Path
    permission_template: str


EXECUTORS: dict[str, ExecutorSpec] = {
    "claude-code": ExecutorSpec(
        name="claude-code",
        binary="claude",
        permission_file=Path(".claude") / "settings.json",
        permission_template="claude_settings.json.tmpl",
    ),
}


def get_executor(name: str) -> ExecutorSpec | None:
    return EXECUTORS.get(name)


def supported_executors() -> str:
    return ", ".join(EXECUTORS)
