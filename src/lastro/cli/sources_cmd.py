"""Grupo lastro sources: cadastro, sincronização, listagem e preparo do modelo local."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from lastro.cli import output
from lastro.cli.output import Status
from lastro.cli.state import get_state
from lastro.config.loader import LoadedConfig
from lastro.history import HistoryStore
from lastro.knowledge.models import JsonObject
from lastro.sources.embeddings import EmbeddingModel, FastEmbedModel, prepare_model
from lastro.sources.models import INVALID_SOURCES_INPUT, SourcesError, SyncResult
from lastro.sources.safety import ScannerFactory, gitleaks_scanner
from lastro.sources.sync import SourceService

USAGE_EXIT_CODE = 2
FAILURE_EXIT_CODE = 1
INTERRUPTED_EXIT_CODE = 130

# Substituíveis em testes: o modelo real e o scanner real não rodam na suíte padrão.
model_factory: Callable[[Path], EmbeddingModel] = FastEmbedModel
scanner_factory: ScannerFactory = gitleaks_scanner
model_preparer: Callable[[Path], str] = prepare_model

sources_app = typer.Typer(
    help="Gerencia as fontes documentais do projeto (Markdown, texto, HTML e PDF).",
    no_args_is_help=True,
)
model_app = typer.Typer(help="Modelo local de embeddings das fontes.", no_args_is_help=True)
sources_app.add_typer(model_app, name="model")

JsonOption = Annotated[bool, typer.Option("--json", help="Saída em JSON estável.")]


def _echo_json(data: object) -> None:
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def project_names(loaded: LoadedConfig) -> tuple[str, ...]:
    """Nome aceito em --project: a pasta do lastro.toml ou o project_id configurado."""
    return (loaded.project_dir.name, loaded.require_project_id())


def build_service(loaded: LoadedConfig, store: HistoryStore) -> SourceService:
    from lastro.sources.conflicts import declared_facts_hook

    root = loaded.private_data_root
    return SourceService(
        store.sources,
        project_names=project_names(loaded),
        model_factory=lambda: model_factory(root),
        scanner_factory=scanner_factory,
        publish_hook=declared_facts_hook(store.project_id),
    )


def _fail(error: SourcesError, as_json: bool) -> typer.Exit:
    if as_json:
        _echo_json(error.as_json())
    else:
        output.error(f"{error.code}: {error.message}")
    code = USAGE_EXIT_CODE if error.code == INVALID_SOURCES_INPUT else FAILURE_EXIT_CODE
    return typer.Exit(code=code)


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def render_sync(result: SyncResult, label: str) -> list[str]:
    files = result.files
    status = Status.OK if result.status == "ok" else Status.WARN
    headline = "sincronizada" if result.status == "ok" else "sincronizada parcialmente"
    lines = [
        f"{output.icon(status)} Fonte {label} {headline}: geração {result.generation}",
        "  Arquivos: "
        + ", ".join(
            (
                _plural(files.added, "adicionado", "adicionados"),
                _plural(files.updated, "atualizado", "atualizados"),
                _plural(files.removed, "removido", "removidos"),
                _plural(files.unchanged, "inalterado", "inalterados"),
                _plural(files.excluded, "excluído", "excluídos"),
                _plural(files.failed, "com falha", "com falha"),
            )
        ),
        f"  Chunks ativos: {result.active_chunks}. Tempo: {result.duration_seconds:.1f}s",
    ]
    if result.reasons:
        reasons = ", ".join(f"{key}={value}" for key, value in sorted(result.reasons.items()))
        lines.append(f"  Motivos: {reasons}")
    lines.extend(f"  Aviso: {warning}" for warning in result.warnings)
    return lines


def _context(ctx: typer.Context) -> tuple[LoadedConfig, HistoryStore]:
    state = get_state(ctx)
    loaded = state.require_history_config()
    return loaded, state.open_history(loaded)


def _finish(result: SyncResult, label: str, as_json: bool) -> None:
    if as_json:
        _echo_json(result.as_json())
    else:
        for line in render_sync(result, label):
            output.info(line)
    if result.status != "ok":
        raise typer.Exit(code=FAILURE_EXIT_CODE)


@sources_app.command("add")
def add(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="Pasta local com a documentação.")],
    project: Annotated[
        str, typer.Option("--project", help="Nome (pasta do lastro.toml) ou ID do projeto.")
    ],
    as_json: JsonOption = False,
) -> None:
    """Cadastra uma pasta de documentação e indexa a primeira geração."""
    loaded, store = _context(ctx)
    try:
        result = build_service(loaded, store).add(path, project)
    except SourcesError as error:
        raise _fail(error, as_json) from None
    except KeyboardInterrupt:
        output.error("Interrompido; a geração ativa anterior foi preservada.")
        raise typer.Exit(code=INTERRUPTED_EXIT_CODE) from None
    label = next(
        (item.label for item in store.sources.statuses() if item.id == result.source_ids[0]),
        result.source_ids[0],
    )
    _finish(result, f"'{label}' ({result.source_ids[0]})", as_json)


@sources_app.command("sync")
def sync(
    ctx: typer.Context,
    source_id: Annotated[
        str | None, typer.Option("--source-id", help="Sincroniza só esta fonte.")
    ] = None,
    rebuild: Annotated[
        bool, typer.Option("--rebuild", help="Refaz extração, chunks e vetores.")
    ] = False,
    as_json: JsonOption = False,
) -> None:
    """Reindexa arquivos alterados e remove do índice os apagados."""
    loaded, store = _context(ctx)
    try:
        result = build_service(loaded, store).sync(source_id, rebuild)
    except SourcesError as error:
        raise _fail(error, as_json) from None
    except KeyboardInterrupt:
        output.error("Interrompido; a geração ativa anterior foi preservada.")
        raise typer.Exit(code=INTERRUPTED_EXIT_CODE) from None
    label = source_id if source_id is not None else "todas as fontes"
    _finish(result, label, as_json)


@sources_app.command("list")
def list_sources(ctx: typer.Context, as_json: JsonOption = False) -> None:
    """Lista as fontes cadastradas, sem caminhos absolutos."""
    _, store = _context(ctx)
    statuses = store.sources.statuses()
    if as_json:
        data: JsonObject = {"sources": [item.as_json() for item in statuses]}
        _echo_json(data)
        return
    if not statuses:
        output.info("Nenhuma fonte documental cadastrada.")
        return
    header = ("ID", "RÓTULO", "PRONTOS", "EXCLUÍDOS", "FALHAS", "CHUNKS", "ÚLTIMA SYNC")
    rows = [header] + [
        (
            item.id,
            item.label,
            str(item.ready),
            str(item.excluded),
            str(item.failed),
            str(item.active_chunks),
            item.last_sync_at or "nunca",
        )
        for item in statuses
    ]
    widths = [max(len(row[index]) for row in rows) for index in range(len(header))]
    for row in rows:
        output.info(
            "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        )


@model_app.command("prepare")
def prepare(ctx: typer.Context, as_json: JsonOption = False) -> None:
    """Baixa e valida o modelo local no private data root (operação explícita)."""
    loaded = get_state(ctx).require_history_config()
    try:
        fingerprint = model_preparer(loaded.private_data_root)
    except SourcesError as error:
        raise _fail(error, as_json) from None
    except KeyboardInterrupt:
        output.error("Interrompido; o cache anterior do modelo foi preservado.")
        raise typer.Exit(code=INTERRUPTED_EXIT_CODE) from None
    if as_json:
        _echo_json({"status": "ok", "fingerprint": fingerprint})
        return
    output.info(f"{output.icon(Status.OK)} Modelo local preparado (fingerprint {fingerprint[:12]})")
