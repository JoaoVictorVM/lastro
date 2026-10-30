"""Comando lastro init: cria a configuração inicial do projeto e o private data root."""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from lastro.cli import output
from lastro.config.errors import ConfigError
from lastro.config.loader import load_config
from lastro.config.schema import PROFILE_NAME_MAX_LENGTH, is_http_url, is_valid_profile_name
from lastro.paths import ensure_writable, is_inside_git_tree, resolve_private_data_root
from lastro.scaffold.files import (
    CONFIG_FILE,
    InitAnswers,
    ProjectIdMigration,
    add_project_id,
    write_scaffold,
)


def validate_profile_name(value: str) -> str:
    value = value.strip()
    if not is_valid_profile_name(value):
        raise typer.BadParameter(
            "use letras minúsculas, dígitos, - e _, começando por letra ou dígito, "
            f"com até {PROFILE_NAME_MAX_LENGTH} caracteres (ex.: homolog)"
        )
    return value


def validate_url(value: str) -> str:
    value = value.strip()
    if not is_http_url(value):
        raise typer.BadParameter("informe uma URL http:// ou https:// com host")
    return value


def validate_tenant(value: str) -> str:
    value = value.strip()
    if not value:
        raise typer.BadParameter("o tenant não pode ser vazio")
    return value


def _option_callback(validator: Callable[[str], str]) -> Callable[[str | None], str | None]:
    def callback(value: str | None) -> str | None:
        return None if value is None else validator(value)

    return callback


QUESTIONS: tuple[tuple[str, Callable[[str], str]], ...] = (
    ("Nome do perfil", validate_profile_name),
    ("URL do sistema alvo", validate_url),
    ("URL de login", validate_url),
    ("Tenant", validate_tenant),
)


def _ask(index: int, given: str | None) -> str:
    if given is not None:
        return given
    label, validator = QUESTIONS[index]
    answer: str = typer.prompt(label, value_proc=validator)
    return answer


def _private_root_for(project_dir: Path) -> Path:
    # Com um lastro.toml válido preservado, a sobrescrita dele vale; senão, o padrão.
    config_path = project_dir / CONFIG_FILE
    override: str | None = None
    if config_path.is_file():
        try:
            override = load_config(config_path).private_data_root
        except ConfigError:
            override = None
    return resolve_private_data_root(override, project_dir)


def _migrate_project_id(project_dir: Path) -> None:
    config_path = project_dir / CONFIG_FILE
    try:
        load_config(config_path)
    except ConfigError:
        output.error(f"{CONFIG_FILE.as_posix()} inválido: project_id não foi adicionado.")
        return
    try:
        migration = add_project_id(config_path)
    except (OSError, ValueError) as exc:
        output.error(f"Não foi possível adicionar project_id em {CONFIG_FILE.as_posix()}: {exc}")
        return
    if migration is ProjectIdMigration.ADDED:
        output.info(f"Migrado: {CONFIG_FILE.as_posix()} (project_id adicionado)")
    else:
        output.info(f"project_id existente preservado em {CONFIG_FILE.as_posix()}")


def init(
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            help="Nome do perfil (ex.: homolog).",
            callback=_option_callback(validate_profile_name),
        ),
    ] = None,
    target_url: Annotated[
        str | None,
        typer.Option(
            "--target-url",
            help="URL base do sistema sob teste.",
            callback=_option_callback(validate_url),
        ),
    ] = None,
    login_url: Annotated[
        str | None,
        typer.Option(
            "--login-url", help="URL da página de login.", callback=_option_callback(validate_url)
        ),
    ] = None,
    tenant: Annotated[
        str | None,
        typer.Option(
            "--tenant",
            help="Identificador do tenant.",
            callback=_option_callback(validate_tenant),
        ),
    ] = None,
) -> None:
    """Cria lastro.toml, .env.example, a permissão do executor, missions/ e o private data root.

    Em um projeto existente, acrescenta o project_id ausente ao lastro.toml sem mudar os demais
    valores.
    """
    answers = InitAnswers(
        profile_name=_ask(0, name),
        target_url=_ask(1, target_url),
        login_url=_ask(2, login_url),
        tenant_id=_ask(3, tenant),
    )
    project_dir = Path.cwd()
    result = write_scaffold(project_dir, answers)

    for relative in result.created:
        output.info(f"Criado: {relative.as_posix()}")
    for relative in result.preserved:
        output.info(f"Preservado (já existia): {relative.as_posix()}")
    if CONFIG_FILE in result.preserved:
        _migrate_project_id(project_dir)

    root = _private_root_for(project_dir)
    try:
        ensure_writable(root)
    except OSError as exc:
        output.error(f"Não foi possível criar o private data root em {root}: {exc}")
        raise typer.Exit(code=1) from None
    output.info(f"Private data root: {root}")
    if is_inside_git_tree(root):
        output.error(
            f"Aviso: o private data root {root} está dentro de um repositório Git; "
            "defina private_data_root em um local fora do repositório."
        )

    if result.preserved:
        output.error("Arquivos existentes foram preservados; nada foi sobrescrito.")
        raise typer.Exit(code=1)
    output.info("Próximo passo: preencha .env e rode lastro doctor")
