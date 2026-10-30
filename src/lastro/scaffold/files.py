"""Geração dos arquivos do lastro init a partir dos templates do pacote."""

import json
import os
import tomllib
import uuid
from dataclasses import dataclass, field
from enum import Enum
from importlib.resources import files
from pathlib import Path
from string import Template

from lastro.config.schema import DEFAULT_EXECUTOR, is_canonical_project_id, new_project_id
from lastro.executors import ExecutorSpec, get_executor

CONFIG_FILE = Path("lastro.toml")
ENV_EXAMPLE_FILE = Path(".env.example")
MISSIONS_MARKER = Path("missions") / ".gitkeep"
CREDENTIAL_PREFIX = "LASTRO_"


@dataclass(frozen=True)
class InitAnswers:
    profile_name: str
    target_url: str
    login_url: str
    tenant_id: str
    project_id: str = field(default_factory=new_project_id)


@dataclass
class ScaffoldResult:
    created: list[Path] = field(default_factory=list[Path])
    preserved: list[Path] = field(default_factory=list[Path])


def credential_variable_names(profile_name: str) -> tuple[str, str]:
    # O prefixo garante um nome de variável válido mesmo quando o perfil começa com dígito.
    base = CREDENTIAL_PREFIX + profile_name.upper().replace("-", "_")
    return f"{base}_USERNAME", f"{base}_PASSWORD"


def _template(name: str) -> Template:
    text = files("lastro.scaffold").joinpath("templates", name).read_text(encoding="utf-8")
    return Template(text)


def _toml_string(value: str) -> str:
    # Strings JSON são strings básicas válidas em TOML (mesmas regras de escape).
    return json.dumps(value, ensure_ascii=False)


def executor_spec(executor: str = DEFAULT_EXECUTOR) -> ExecutorSpec:
    spec = get_executor(executor)
    if spec is None:
        raise ValueError(f"executor não suportado: {executor}")
    return spec


def render_files(answers: InitAnswers, executor: str = DEFAULT_EXECUTOR) -> dict[Path, str]:
    username_env, password_env = credential_variable_names(answers.profile_name)
    toml_values = {
        "project_id": _toml_string(answers.project_id),
        "profile_name": answers.profile_name,
        "target_url": _toml_string(answers.target_url),
        "login_url": _toml_string(answers.login_url),
        "tenant_id": _toml_string(answers.tenant_id),
        "username_env": _toml_string(username_env),
        "password_env": _toml_string(password_env),
    }
    env_values = {
        "profile_name": answers.profile_name,
        "username_env": username_env,
        "password_env": password_env,
    }
    spec = executor_spec(executor)
    return {
        CONFIG_FILE: _template("lastro.toml.tmpl").substitute(toml_values),
        ENV_EXAMPLE_FILE: _template("env.example.tmpl").substitute(env_values),
        spec.permission_file: _template(spec.permission_template).template,
        MISSIONS_MARKER: "",
    }


def write_scaffold(
    project_dir: Path, answers: InitAnswers, executor: str = DEFAULT_EXECUTOR
) -> ScaffoldResult:
    """Cria os arquivos ausentes; arquivos existentes nunca são sobrescritos."""
    result = ScaffoldResult()
    for relative, content in render_files(answers, executor).items():
        target = project_dir / relative
        if target.exists():
            result.preserved.append(relative)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
        except FileExistsError:
            result.preserved.append(relative)
            continue
        result.created.append(relative)
    return result


class ProjectIdMigration(Enum):
    ADDED = "added"
    PRESERVED = "preserved"


PROJECT_ID_LINE_COMMENT = (
    "# Identidade estável do projeto, comum a todos os perfis. Nomeia a pasta do histórico no\n"
    "# private data root; não altere depois de criada.\n"
)


def _insertion_offset(lines: list[str]) -> int:
    # Chaves de nível raiz precisam vir antes da primeira tabela; logo depois do bloco de
    # comentários iniciais ainda é sempre nível raiz.
    index = 0
    while index < len(lines) and lines[index].lstrip().startswith("#"):
        index += 1
    if 0 < index < len(lines) and not lines[index].strip():
        index += 1
    return index


def add_project_id(config_path: Path, project_id: str | None = None) -> ProjectIdMigration:
    """Acrescenta project_id a um lastro.toml existente sem alterar nenhum outro valor.

    A escrita é atômica e só acontece depois de provar que o resultado, lido de novo, difere do
    original apenas pelo campo novo. Um project_id existente é sempre preservado.
    """
    raw = config_path.read_bytes()
    text = raw.decode("utf-8")
    original = tomllib.loads(text)
    if "project_id" in original:
        return ProjectIdMigration.PRESERVED
    value = project_id or new_project_id()
    if not is_canonical_project_id(value):
        raise ValueError("project_id deve ser um UUID canônico em minúsculas")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    offset = _insertion_offset(lines)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += newline
    block = (
        PROJECT_ID_LINE_COMMENT.replace("\n", newline)
        + f"project_id = {_toml_string(value)}{newline}{newline}"
    )
    migrated = "".join(lines[:offset]) + block + "".join(lines[offset:])
    expected = {**original, "project_id": value}
    if tomllib.loads(migrated) != expected:
        raise ValueError("a migração alteraria outros valores do lastro.toml")
    temporary = config_path.with_name(f".{config_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(migrated.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, config_path)
    finally:
        temporary.unlink(missing_ok=True)
    return ProjectIdMigration.ADDED
