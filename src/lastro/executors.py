"""Executores suportados: binário do doctor, permissão e instruções geradas pelo init."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ExecutorSpec:
    name: str
    binary: str
    permission_file: Path
    permission_template: str
    instruction_file: Path
    instruction_template: str


EXECUTORS: dict[str, ExecutorSpec] = {
    "claude-code": ExecutorSpec(
        name="claude-code",
        binary="claude",
        permission_file=Path(".claude") / "settings.json",
        permission_template="claude_settings.json.tmpl",
        instruction_file=Path("CLAUDE.md"),
        instruction_template="CLAUDE.md.tmpl",
    ),
}


def get_executor(name: str) -> ExecutorSpec | None:
    return EXECUTORS.get(name)


def supported_executors() -> str:
    return ", ".join(EXECUTORS)
