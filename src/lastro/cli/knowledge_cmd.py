"""Grupo lastro knowledge: consulta e gestão humana da memória, com revisão esperada."""

import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from lastro.cli import output
from lastro.cli.state import HISTORY_ERROR_EXIT_CODE, get_state
from lastro.history import HistoryError, HistoryStore, KnowledgeError
from lastro.knowledge import (
    EditDocument,
    KnowledgeDetail,
    KnowledgeEntry,
    MemoryOutcome,
    MutationResult,
    ValidationState,
)
from lastro.knowledge.retrieval import staleness
from lastro.timeutil import parse_timestamp, to_display_zone, utc_now

USAGE_EXIT_CODE = 2
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
EMPTY_MESSAGE = "Nenhuma entrada de memória encontrada."
HEADERS = ("ID", "REV", "ESTADO", "FONTE", "FLUXO", "VERIFICADO", "STALE", "AFIRMAÇÃO")

# Relógio substituível para que a staleness exibida seja testável.
clock: Callable[[], datetime] = utc_now

knowledge_app = typer.Typer(
    help="Gerencia a base de conhecimento do sistema alvo.", no_args_is_help=True
)

JsonOption = Annotated[bool, typer.Option("--json", help="Saída em JSON redigido.")]
RevisionOption = Annotated[
    int, typer.Option("--expected-revision", min=1, help="Revisão lida antes da alteração.")
]
ReasonOption = Annotated[str, typer.Option("--reason", help="Motivo registrado no histórico.")]


def _store(ctx: typer.Context) -> HistoryStore:
    state = get_state(ctx)
    return state.open_history(state.require_history_config())


def _fail(error: HistoryError) -> typer.Exit:
    if isinstance(error, KnowledgeError):
        output.error(f"{error.code}: {error.detail}")
        if error.current_revision is not None:
            output.error(f"Revisão corrente: {error.current_revision}")
    else:
        output.error(str(error))
    return typer.Exit(code=HISTORY_ERROR_EXIT_CODE)


def _echo_json(data: object) -> None:
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def entry_json(entry: KnowledgeEntry, now: datetime) -> dict[str, Any]:
    verified = parse_timestamp(entry.last_verified_at) if entry.last_verified_at else None
    stale = staleness(verified, now, entry.system_version, None)
    data = asdict(entry)
    data["category"] = entry.category.value
    data["validation_state"] = entry.validation_state.value
    data["source_type"] = entry.source_type.value
    data["conflict_ids"] = list(entry.conflict_ids)
    data["stale"] = stale.stale
    data["stale_reasons"] = list(stale.reasons)
    data["version_unknown"] = stale.version_unknown
    return data


def _when(value: str | None, timezone: str) -> str:
    if value is None:
        return "nunca"
    return to_display_zone(value, timezone).strftime("%Y-%m-%d %H:%M")


def _table(rows: list[tuple[str, ...]]) -> list[str]:
    widths = [max(len(row[index]) for row in rows) for index in range(len(HEADERS))]
    return [
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in rows
    ]


@knowledge_app.command("list")
def list_entries(
    ctx: typer.Context,
    flow: Annotated[str | None, typer.Option("--flow", help="Somente este fluxo.")] = None,
    state: Annotated[
        str | None,
        typer.Option("--state", help="observed, user_provided ou contested."),
    ] = None,
    stale_only: Annotated[
        bool, typer.Option("--stale", help="Somente entradas que exigem revalidação por idade.")
    ] = False,
    page: Annotated[int, typer.Option("--page", min=1, help="Página, a partir de 1.")] = 1,
    page_size: Annotated[
        int, typer.Option("--page-size", min=1, max=MAX_PAGE_SIZE, help="Entradas por página.")
    ] = DEFAULT_PAGE_SIZE,
    as_json: JsonOption = False,
) -> None:
    """Lista afirmações com estado, fonte, escopo e última verificação."""
    selected: ValidationState | None = None
    if state is not None:
        try:
            selected = ValidationState(state)
        except ValueError:
            output.error("estado desconhecido (use observed, user_provided ou contested)")
            raise typer.Exit(code=USAGE_EXIT_CODE) from None
    store = _store(ctx)
    now = clock()
    entries = [entry_json(item, now) for item in store.list_knowledge(flow=flow, state=selected)]
    if stale_only:
        entries = [item for item in entries if item["stale"]]
    start = (page - 1) * page_size
    shown = entries[start : start + page_size]
    if as_json:
        _echo_json({"entries": shown, "page": page, "page_size": page_size, "total": len(entries)})
        return
    if not shown:
        output.info(EMPTY_MESSAGE)
        return
    rows: list[tuple[str, ...]] = [HEADERS]
    for item in shown:
        rows.append(
            (
                item["id"],
                str(item["revision"]),
                item["validation_state"],
                item["source_type"],
                item["flow"],
                _when(item["last_verified_at"], store.timezone),
                "sim" if item["stale"] else "não",
                item["statement"],
            )
        )
    for line in _table(rows):
        output.info(line)
    for item in shown:
        if item["conflict_ids"]:
            output.info(f"{item['id']} contestada; alternativas: {', '.join(item['conflict_ids'])}")
    if start + page_size < len(entries):
        output.info(f"Mais entradas: use --page {page + 1}.")


def detail_json(detail: KnowledgeDetail, now: datetime) -> dict[str, Any]:
    return {
        "entry": entry_json(detail.entry, now),
        "evidence": [asdict(item) for item in detail.evidence],
        "history": [asdict(item) for item in detail.history],
        "alternatives": [entry_json(item, now) for item in detail.alternatives],
        "mutations": [
            asdict(item) | {"operation": item.operation.value} for item in detail.mutations
        ],
    }


@knowledge_app.command("show")
def show(
    ctx: typer.Context,
    entry_id: Annotated[str, typer.Argument(help="Id da entrada.")],
    as_json: JsonOption = False,
) -> None:
    """Mostra fato, evidências, versões, histórico e alternativas contestadas."""
    store = _store(ctx)
    try:
        detail = store.knowledge_detail(entry_id)
    except HistoryError as error:
        raise _fail(error) from None
    data = detail_json(detail, clock())
    if as_json:
        _echo_json(data)
        return
    entry = data["entry"]
    scope = entry["scope"]
    lines = [
        f"{entry['id']} (revisão {entry['revision']})",
        f"Afirmação: {entry['statement']}",
        f"Estado: {entry['validation_state']} · fonte: {entry['source_type']} "
        f"({entry['source_ref']})",
        f"Fluxo: {entry['flow']} · categoria: {entry['category']}",
        "Escopo: "
        + ", ".join(f"{key}={value}" for key, value in scope.items() if value is not None),
        f"Versão do sistema: {entry['system_version'] or 'desconhecida'}",
        f"Última verificação: {_when(entry['last_verified_at'], store.timezone)}"
        + (f" (stale: {', '.join(entry['stale_reasons'])})" if entry["stale"] else ""),
        "Fato: " + json.dumps(entry["fact"], ensure_ascii=False, sort_keys=True),
        "Evidências:",
    ]
    for item in detail.evidence:
        ref = item.observation_id or item.artifact_id or item.clarification_id
        lines.append(f"- {item.run_id}: {ref}")
    if detail.alternatives:
        lines.append("Alternativas contestadas:")
        lines += [f"- {item.id}: {item.statement}" for item in detail.alternatives]
    lines.append("Histórico:")
    for item in detail.history:
        lines.append(
            f"- {_when(item.created_at, store.timezone)} {item.operation or 'legado'} "
            f"por {item.actor or '-'} (revisão {item.revision or '-'})"
        )
    pending = [item for item in detail.mutations if item.status == "pending"]
    if pending:
        lines.append(f"Alterações adiadas: {', '.join(item.id for item in pending)}")
    for line in lines:
        output.info(line)


def _report(result: MutationResult, verb: str, as_json: bool) -> None:
    now = clock()
    if as_json:
        _echo_json(
            {
                "status": result.status,
                "mutation_id": result.mutation_id,
                "entry": entry_json(result.entry, now),
            }
        )
        return
    if result.status == "deferred":
        output.info(
            f"Pedido registrado ({result.mutation_id}), ainda não aplicado: "
            f"uma run em andamento usa {result.entry.id}. Será aplicado ao fim dela."
        )
        return
    output.info(f"{result.entry.id} {verb}; revisão atual {result.entry.revision}.")


@knowledge_app.command("edit")
def edit(
    ctx: typer.Context,
    entry_id: Annotated[str, typer.Argument(help="Id da entrada.")],
    file: Annotated[
        Path, typer.Option("--file", help="JSON com fact e, opcionalmente, discard_alternatives.")
    ],
    expected_revision: RevisionOption,
    reason: ReasonOption,
    as_json: JsonOption = False,
) -> None:
    """Corrige o fato criando uma revisão humana; versões anteriores ficam no histórico."""
    try:
        document = EditDocument.model_validate_json(file.read_text(encoding="utf-8"))
    except OSError as exc:
        output.error(f"Não foi possível ler {file}: {exc}")
        raise typer.Exit(code=USAGE_EXIT_CODE) from None
    except ValidationError as exc:
        output.error(f"invalid_knowledge: {exc.errors()[0]['msg']}")
        raise typer.Exit(code=USAGE_EXIT_CODE) from None
    store = _store(ctx)
    try:
        result = store.edit_knowledge(entry_id, document, expected_revision, reason)
    except HistoryError as error:
        raise _fail(error) from None
    _report(result, "corrigida", as_json)


@knowledge_app.command("discard")
def discard(
    ctx: typer.Context,
    entry_id: Annotated[str, typer.Argument(help="Id da entrada.")],
    expected_revision: RevisionOption,
    reason: ReasonOption,
    as_json: JsonOption = False,
) -> None:
    """Descarte lógico: a entrada sai da busca e permanece auditável."""
    store = _store(ctx)
    try:
        result = store.discard_knowledge(entry_id, expected_revision, reason)
    except HistoryError as error:
        raise _fail(error) from None
    _report(result, "descartada", as_json)


@knowledge_app.command("revalidate")
def revalidate(
    ctx: typer.Context,
    entry_id: Annotated[str, typer.Argument(help="Id da entrada.")],
    expected_revision: RevisionOption,
    reason: ReasonOption,
    system_version: Annotated[
        str | None,
        typer.Option("--system-version", help="Versão conferida; omitida continua desconhecida."),
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """Atestado humano datado, sem abrir browser nem repetir escrita no alvo."""
    store = _store(ctx)
    try:
        result = store.attest_knowledge(entry_id, expected_revision, reason, system_version)
    except HistoryError as error:
        raise _fail(error) from None
    _report(result, "revalidada por atestado humano", as_json)


def outcome_json(outcome: MemoryOutcome) -> dict[str, Any]:
    return asdict(outcome) | {"outcome": outcome.outcome.value}


@knowledge_app.command("consolidate")
def consolidate(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run terminal deste projeto.")],
    as_json: JsonOption = False,
) -> None:
    """Retry idempotente da consolidação pendente e de eventual fallback privado."""
    store = _store(ctx)
    try:
        outcome = store.consolidate_memory(run_id)
    except HistoryError as error:
        raise _fail(error) from None
    if as_json:
        _echo_json(outcome_json(outcome))
        return
    output.info(
        f"Memória da run {run_id}: {outcome.outcome.value} "
        f"(alterações {outcome.changed_count}, pendências {outcome.pending_count}, "
        f"revisão {outcome.revision})."
    )
