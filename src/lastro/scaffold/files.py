"""Geração dos arquivos do lastro init a partir dos templates do pacote."""

import hashlib
import json
import os
import tomllib
import uuid
from dataclasses import dataclass, field
from enum import Enum
from importlib.resources import files
from pathlib import Path
from string import Template
from typing import Any, cast

from lastro.config.schema import DEFAULT_EXECUTOR, is_canonical_project_id, new_project_id
from lastro.executors import ExecutorSpec, get_executor
from lastro.mission.bundle import BUNDLE_DIR, PREVIOUS_SHA256, bundle_files, instruction_problems
from lastro.skills import normalize

CONFIG_FILE = Path("lastro.toml")
ENV_EXAMPLE_FILE = Path(".env.example")
MISSIONS_MARKER = Path("missions") / ".gitkeep"
MISSION_EXAMPLE = Path("missions") / "example.md"
CREDENTIAL_PREFIX = "LASTRO_"
HOOK_COMMAND = "lastro-permission-hook"
NATIVE_ENV = (
    "\n# Chave da API Anthropic do executor nativo. Usada só pelo processo do Lastro.\n"
    "ANTHROPIC_API_KEY="
)
NATIVE_BLOCK = """

# Executor nativo: modelo, limites de saída e tetos de custo em USD (texto decimal).
# A chave fica só no .env (ANTHROPIC_API_KEY), nunca aqui.
[profiles.${profile_name}.native]
model = "claude-sonnet-5-5"
max_output_tokens = 2048
cost_cap_usd = "2.00"
suite_cost_cap_usd = "30.00"
context_token_limit = 64000
# Modelo por nó, opcional: observe = "claude-haiku-5-5"
[profiles.${profile_name}.native.node_models]"""
REQUIRED_HOOKS = ("PreToolUse", "Notification")
# Versões publicadas das settings do executor; cópias idênticas recebem os hooks novos no init.
PREVIOUS_SETTINGS_SHA256 = frozenset(
    {
        "c15c4a0dfb181473a700e784be149a13a742afc51a2ef202421f02ec3cad1df1",
        "0a246487125c41d4b4fd504416ae42e9ac6134f0a9c92e66a25e461ce5cd7538",
        "f729a2e8a57db410b5b394e4d0a07f31905c15c8fdfceadea48203cf8c65de06",
    }
)


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
    updated: list[Path] = field(default_factory=list[Path])
    diagnostics: list[str] = field(default_factory=list[str])


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
    spec = executor_spec(executor)
    native_block = (
        Template(NATIVE_BLOCK).substitute(profile_name=answers.profile_name)
        if spec.embedded
        else ""
    )
    toml_values |= {"executor": _toml_string(spec.name), "native_block": native_block}
    env_values = {
        "profile_name": answers.profile_name,
        "username_env": username_env,
        "password_env": password_env,
        "native_env": NATIVE_ENV if spec.embedded else "",
    }
    rendered = {
        CONFIG_FILE: _template("lastro.toml.tmpl").substitute(toml_values),
        ENV_EXAMPLE_FILE: _template("env.example.tmpl").substitute(env_values),
        MISSIONS_MARKER: "",
        MISSION_EXAMPLE: _template("mission.md.tmpl").template,
        **bundle_files(),
    }
    external = spec.external
    if external is None:
        return rendered
    return {
        **rendered,
        external.permission_file: _template(external.permission_template).template,
        external.instruction_file: _template(external.instruction_template).template,
        Path(".mcp.json"): json.dumps(
            {
                "mcpServers": {
                    "lastro": {
                        "command": "lastro-browser",
                        "args": ["--profile", answers.profile_name],
                    }
                }
            },
            indent=2,
        )
        + "\n",
    }


def _replace_atomically(target: Path, content: str) -> None:
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _managed_existing(
    project_dir: Path, relative: Path, content: str, result: ScaffoldResult
) -> None:
    """Arquivo do bundle já existente: igual fica, versão antiga conhecida é atualizada."""
    target = project_dir / relative
    current = normalize(target.read_bytes())
    if current == normalize(content.encode("utf-8")):
        result.preserved.append(relative)
        return
    digest = hashlib.sha256(current).hexdigest()
    if digest in PREVIOUS_SHA256.get(relative.name, frozenset()):
        _replace_atomically(target, content)
        result.updated.append(relative)
        return
    result.preserved.append(relative)
    package = files("lastro.skills").joinpath(relative.name)
    result.diagnostics.append(
        f"{relative.as_posix()} foi modificado localmente e não foi sobrescrito; compare com "
        f"git diff --no-index {package} {relative.as_posix()}"
    )


def _items(value: object) -> list[dict[str, Any]]:
    entries = cast(list[object], value) if isinstance(value, list) else []
    return [cast(dict[str, Any], item) for item in entries if isinstance(item, dict)]


def missing_hooks(settings: object) -> list[str]:
    """Eventos do executor sem o hook do Lastro, que sustenta catálogo e prompt de aprovação."""
    data = cast(dict[str, Any], settings) if isinstance(settings, dict) else {}
    hooks_value: object = data.get("hooks")
    hooks = cast(dict[str, Any], hooks_value) if isinstance(hooks_value, dict) else {}
    missing: list[str] = []
    for event in REQUIRED_HOOKS:
        commands = [
            hook.get("command")
            for group in _items(hooks.get(event))
            for hook in _items(group.get("hooks"))
        ]
        if HOOK_COMMAND not in commands:
            missing.append(event)
    return missing


def _managed_settings(
    project_dir: Path, relative: Path, content: str, result: ScaffoldResult
) -> None:
    """Settings existentes: versão publicada é atualizada; edição local só é diagnosticada."""
    target = project_dir / relative
    current = normalize(target.read_bytes())
    if current == normalize(content.encode("utf-8")):
        result.preserved.append(relative)
        return
    if hashlib.sha256(current).hexdigest() in PREVIOUS_SETTINGS_SHA256:
        _replace_atomically(target, content)
        result.updated.append(relative)
        return
    result.preserved.append(relative)
    try:
        missing = missing_hooks(json.loads(current.decode("utf-8")))
    except ValueError:
        missing = list(REQUIRED_HOOKS)
    if missing:
        result.diagnostics.append(
            f"{relative.as_posix()} foi modificado localmente e não foi sobrescrito; adicione "
            f"o hook {HOOK_COMMAND} em {', '.join(missing)} para o catálogo e o prompt de "
            "aprovação"
        )


def write_scaffold(
    project_dir: Path, answers: InitAnswers, executor: str = DEFAULT_EXECUTOR
) -> ScaffoldResult:
    """Cria os arquivos ausentes; arquivos existentes nunca são sobrescritos.

    A exceção são arquivos do bundle idênticos a uma versão publicada anterior, trocados
    atomicamente pela versão atual.
    """
    result = ScaffoldResult()
    external = executor_spec(executor).external
    for relative, content in render_files(answers, executor).items():
        target = project_dir / relative
        if target.exists():
            if relative.parent == BUNDLE_DIR:
                _managed_existing(project_dir, relative, content, result)
            elif external is not None and relative == external.permission_file:
                _managed_settings(project_dir, relative, content, result)
            else:
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
    if external is not None and external.instruction_file in result.preserved:
        instructions = external.instruction_file
        result.diagnostics += [
            problem + "; ajuste o arquivo para apontar para o bundle"
            for problem in instruction_problems(project_dir / instructions)
        ]
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
