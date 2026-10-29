"""Geração dos arquivos do lastro init a partir dos templates do pacote."""

import json
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from string import Template

from lastro.config.schema import DEFAULT_EXECUTOR
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
