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
    # Consumidores do formato .mcp.json; o Codex descreve o servidor na própria configuração.
    uses_mcp_json: bool = True


@dataclass(frozen=True)
class ExecutorSpec:
    name: str
    external: ExternalRuntime | None = None
    install_hint: str = ""
    # Pode rodar uma tentativa de reprodução sem terminal interativo.
    headless: bool = True

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
        install_hint="npm install -g @anthropic-ai/claude-code",
    ),
    "codex": ExecutorSpec(
        name="codex",
        external=ExternalRuntime(
            binary="codex",
            permission_file=Path(".codex") / "config.toml",
            permission_template="codex_config.toml.tmpl",
            instruction_file=Path("AGENTS.md"),
            instruction_template="AGENTS.md.tmpl",
            uses_mcp_json=False,
        ),
        install_hint="npm install -g @openai/codex",
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
