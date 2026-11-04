"""Executores suportados: externos dependem de binário e arquivos gerados; embutidos, não."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ExternalRuntime:
    binary: str
    permission_file: Path
    permission_template: str
    instruction_file: Path
    instruction_template: str


@dataclass(frozen=True)
class ExecutorSpec:
    name: str
    external: ExternalRuntime | None = None
    install_hint: str = ""

    @property
    def embedded(self) -> bool:
        return self.external is None


EXECUTORS: dict[str, ExecutorSpec] = {
    "claude-code": ExecutorSpec(
        name="claude-code",
        external=ExternalRuntime(
            binary="claude",
            permission_file=Path(".claude") / "settings.json",
            permission_template="claude_settings.json.tmpl",
            instruction_file=Path("CLAUDE.md"),
            instruction_template="CLAUDE.md.tmpl",
        ),
    ),
    "native": ExecutorSpec(
        name="native",
        install_hint="uv sync --extra native (ou pip install 'lastro[native]')",
    ),
}


def get_executor(name: str) -> ExecutorSpec | None:
    return EXECUTORS.get(name)


def supported_executors() -> str:
    return ", ".join(EXECUTORS)
