"""Grupo lastro run: cria a run durável antes de qualquer interação com o sistema alvo."""

import asyncio
import contextlib
import secrets
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from lastro.change_context.collector import Collector, CollectRequest, SourcePreflightError
from lastro.change_context.git import GitError
from lastro.change_context.issues import (
    IssueImportError,
    fetch_github_issue,
    import_markdown,
    import_text,
    read_issue_file,
)
from lastro.change_context.models import (
    MAX_FLOW_KEY,
    ChangeContextDraft,
    ComparisonMode,
    IssueCapture,
    PreparedContext,
    SourceState,
)
from lastro.change_context.scanner import Gitleaks, IssueSanitizer, ScannerError, available_scanner
from lastro.cli import output
from lastro.cli.state import HISTORY_ERROR_EXIT_CODE, CliState, get_state
from lastro.config.loader import ProjectContext
from lastro.config.source import SourceConfigError, is_valid_ref, resolve_source_root
from lastro.executors import get_executor, supported_executors
from lastro.history import (
    MAX_MISSION_TEXT_LENGTH,
    CreatedRun,
    ExecutorInfo,
    FileMission,
    HistoryError,
    HistoryStore,
    IssueMission,
    Mission,
    TextMission,
)
from lastro.mission import BundleError, instruction_problems, verify_bundle
from lastro.scaffold.files import executor_spec

USAGE_EXIT_CODE = 2
INTERRUPTED_EXIT_CODE = 130
MISSION_SOURCE_MESSAGE = (
    "Informe exatamente uma fonte de missão: --mission, --mission-file, --issue, --issue-file "
    "ou --issue-text."
)

type Handoff = Callable[[CreatedRun], None]


def _no_handoff(created: CreatedRun) -> None:
    return None


# Ponto de entrega para a sessão do executor. Ela só recebe a run depois que o registro, a
# missão e run_started já estão confirmados no histórico.
handoff: Handoff = _no_handoff


def mission_from_options(text: str | None, file: Path | None) -> Mission | None:
    """As duas fontes são exclusivas; ambas ou nenhuma não formam uma missão."""
    if text is not None and file is None:
        return TextMission(text)
    if file is not None and text is None:
        return FileMission(file)
    return None


def _usage(message: str) -> NoReturn:
    output.error(message)
    raise typer.Exit(code=USAGE_EXIT_CODE)


def _check_source_options(
    *,
    source: bool,
    compare: ComparisonMode | None,
    base: str | None,
    pr: str | None,
    flow: str | None,
    fetch: bool,
) -> CollectRequest | None:
    """Combinações de flags validadas antes de qualquer consulta externa."""
    if not source:
        if compare is not None or base is not None or pr is not None or fetch:
            _usage("--compare, --base, --pr and --fetch require --source.")
        return None
    mode = compare or ComparisonMode.WORKING_TREE
    if base is not None and mode in (ComparisonMode.PR, ComparisonMode.LAST_PASSED):
        _usage("--base cannot be used with --compare pr or last-passed.")
    if base is not None and not is_valid_ref(base):
        _usage("--base must be a plain git ref name (no revision syntax).")
    if (pr is not None) != (mode == ComparisonMode.PR):
        _usage("--pr URL is required by, and only valid with, --compare pr.")
    if mode == ComparisonMode.LAST_PASSED and flow is None:
        _usage("--compare last-passed requires --flow.")
    return CollectRequest(mode=mode, base=base, pr_url=pr, flow_key=flow, fetch=fetch)


def _import_issue(
    url: str | None, file: Path | None, text: str | None, sanitizer: IssueSanitizer
) -> IssueCapture:
    """Preflight antes da run: a consulta ao GitHub não toca o sistema sob teste."""
    try:
        if url is not None:
            return fetch_github_issue(url, sanitizer=sanitizer)
        if file is not None:
            return import_markdown(read_issue_file(file), sanitizer=sanitizer)
        assert text is not None
        return import_text(text, sanitizer=sanitizer)
    except IssueImportError as exc:
        _usage(str(exc))


def _collect_source(
    project: ProjectContext, store: HistoryStore, request: CollectRequest
) -> ChangeContextDraft:
    """Preflight de --source: raiz, scanner obrigatório e captura congelada, ainda sem run."""
    settings = project.config.source
    try:
        root = resolve_source_root(settings)
        scanner = Gitleaks()
        scanner.ensure()
        profile = project.profile

        def baseline(flow: str) -> tuple[str, str] | None:
            return store.last_passed_baseline(project.profile_name, profile.tenant_id, flow)

        return Collector(settings, root, scanner=scanner, baseline=baseline).collect(request)
    except (SourceConfigError, ScannerError, SourcePreflightError, IssueImportError) as exc:
        _usage(f"{exc} No run was started; run without --source to continue without code.")
    except GitError:
        _usage("Git context could not be collected safely. No run was started.")


def _report_context(prepared: PreparedContext) -> None:
    if prepared.issue is not None:
        content = prepared.issue.content
        origin = (
            f"{content.repository}#{content.number}"
            if content.repository and content.number
            else content.source_kind.value
        )
        output.info(
            f"Issue: {origin} · sha256 {content.content_sha256[:12]} · "
            f"{content.comments_included}/{content.comments_total} comments · "
            f"scan {prepared.issue.scan_level.value}"
        )
    change = prepared.change
    if change is None:
        return
    output.info(
        f"Change context: {change.source_state.value} · "
        f"{change.comparison.value if change.comparison else '-'} · "
        f"{len(change.files)} changed · {len(change.sources)} sources · "
        f"{len(change.exclusions)} excluded · freshness {change.remote_freshness.value}"
    )
    for warning in change.warnings:
        output.info(f"Warning: {warning}")


def _codex_preflight(project: ProjectContext) -> Any:
    """Antes de criar a run: instalação, login ChatGPT, regras, instruções e bundle."""
    from lastro.codex.launcher import prepare_codex
    from lastro.codex.preflight import CodexPreflightError

    try:
        return prepare_codex(project)
    except CodexPreflightError as exc:
        output.error(str(exc))
        raise typer.Exit(code=USAGE_EXIT_CODE) from None


def _launch_command(state: CliState, run_id: str) -> str:
    options = ""
    if state.config_path is not None:
        options += f' --config "{state.config_path}"'
    if state.profile is not None:
        options += f" --profile {state.profile}"
    return f"lastro{options} run launch --run-id {run_id}"


def _bundle_hash(project_dir: Path, executor: str) -> str:
    """Hash do bundle local; sem bundle íntegro e instruções que o referenciem, não há run."""
    try:
        bundle = verify_bundle(project_dir)
    except BundleError as exc:
        output.error(str(exc))
        raise typer.Exit(code=USAGE_EXIT_CODE) from None
    external = executor_spec(executor).external
    if external is None:
        return bundle.sha256
    problems = instruction_problems(project_dir / external.instruction_file, check_length=False)
    if problems:
        output.error(
            f"Instruções do executor inválidas: {'; '.join(problems)}; rode 'lastro init'."
        )
        raise typer.Exit(code=USAGE_EXIT_CODE)
    return bundle.sha256


run_app = typer.Typer(
    help="Executa uma missão contra o perfil selecionado.",
    no_args_is_help=True,
)


@run_app.command("start")
def start(
    ctx: typer.Context,
    mission: Annotated[
        str | None,
        typer.Option(
            "--mission",
            help=f"Missão em texto (até {MAX_MISSION_TEXT_LENGTH} caracteres).",
        ),
    ] = None,
    mission_file: Annotated[
        Path | None,
        typer.Option("--mission-file", help="Arquivo com a missão (ex.: missions/cadastro.md)."),
    ] = None,
    executor: Annotated[
        str | None,
        typer.Option(
            "--executor",
            help=f"Executor só desta run ({supported_executors()}); padrão: o do lastro.toml.",
        ),
    ] = None,
    issue: Annotated[
        str | None,
        typer.Option("--issue", help="URL https://github.com/<owner>/<repo>/issues/<n>."),
    ] = None,
    issue_file: Annotated[
        Path | None,
        typer.Option("--issue-file", help="Exportação Markdown local de uma issue (até 1 MB)."),
    ] = None,
    issue_text: Annotated[
        str | None, typer.Option("--issue-text", help="Texto de uma issue, sem identidade remota.")
    ] = None,
    source: Annotated[
        bool,
        typer.Option(
            "--source", help="Inclui contexto de código de SOURCE_REPO_PATH ([source] no toml)."
        ),
    ] = False,
    compare: Annotated[
        ComparisonMode | None,
        typer.Option("--compare", help="feature, working-tree (padrão), pr ou last-passed."),
    ] = None,
    base: Annotated[
        str | None, typer.Option("--base", help="Ref base só desta run (ex.: origin/main).")
    ] = None,
    pr: Annotated[
        str | None, typer.Option("--pr", help="URL do pull request para --compare pr.")
    ] = None,
    flow: Annotated[
        str | None,
        typer.Option("--flow", help="Chave estável do fluxo (obrigatória em last-passed)."),
    ] = None,
    fetch: Annotated[
        bool,
        typer.Option("--fetch", help="Consente com git fetch do remote validado antes da coleta."),
    ] = False,
) -> None:
    """Registra a run, imprime o id e a pasta e entrega o controle ao executor.

    Com o Codex, imprime o comando lastro run launch que abre a sessão supervisionada.
    """
    sources = [mission, mission_file, issue, issue_file, issue_text]
    if sum(item is not None for item in sources) != 1:
        _usage(MISSION_SOURCE_MESSAGE)
    if flow is not None and not 1 <= len(flow.strip()) <= MAX_FLOW_KEY:
        _usage(f"--flow must have between 1 and {MAX_FLOW_KEY} characters.")
    request = _check_source_options(
        source=source, compare=compare, base=base, pr=pr, flow=flow, fetch=fetch
    )
    imported = issue is not None or issue_file is not None or issue_text is not None
    chosen: Mission | None = (
        IssueMission(issue_snapshot_id="issue_" + secrets.token_hex(8))
        if imported
        else mission_from_options(mission, mission_file)
    )
    if chosen is None:
        _usage(MISSION_SOURCE_MESSAGE)
    if executor is not None and get_executor(executor) is None:
        output.error(f"Executor não suportado: {executor} (suportados: {supported_executors()}).")
        raise typer.Exit(code=USAGE_EXIT_CODE)
    state = get_state(ctx)
    project = state.require_history_project()
    if executor is not None:
        project = project.with_executor(executor)
    codex = _codex_preflight(project) if project.config.executor == "codex" else None
    bundle_hash = _bundle_hash(project.loaded.project_dir, project.config.executor)
    embedded = executor_spec(project.config.executor).embedded
    prepared = _native_preflight(project) if embedded else None
    capture = (
        _import_issue(issue, issue_file, issue_text, IssueSanitizer(available_scanner()))
        if imported
        else None
    )
    store = state.open_history(project)
    change = _collect_source(project, store, request) if request is not None else None
    if change is None and flow is not None:
        change = ChangeContextDraft(source_state=SourceState.DISABLED, flow_key=flow.strip())
    frozen = (
        PreparedContext(issue=capture, change=change)
        if capture is not None or change is not None
        else None
    )
    try:
        created = store.create_run(
            project,
            chosen,
            ExecutorInfo(name="codex", version=codex.install.version) if codex else None,
            skill_bundle_version=bundle_hash,
            prepared=frozen,
            executor_snapshot=_codex_snapshot(codex) if codex else None,
        )
    except HistoryError as exc:
        output.error(str(exc))
        raise typer.Exit(code=HISTORY_ERROR_EXIT_CODE) from None
    output.info(f"Run: {created.record.id}")
    output.info(f"Pasta: {created.paths.run_dir}")
    if frozen is not None:
        _report_context(frozen)
    if prepared is not None:
        raise typer.Exit(code=_run_native(project, store, created, prepared))
    if codex is not None:
        output.info(f"Executor: codex {codex.install.version}")
        output.info(f"Start Codex with: {_launch_command(state, created.record.id)}")
        return
    handoff(created)


def _codex_snapshot(prepared: Any) -> Any:
    from lastro.codex.launcher import executor_snapshot

    return executor_snapshot(prepared)


@run_app.command("launch")
def launch(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Option("--run-id", help="Run criada por lastro run start.")],
    headless: Annotated[
        bool, typer.Option("--headless", help="Usa codex exec, sem interface interativa.")
    ] = False,
) -> None:
    """Abre o Codex supervisionado para a run; não aceita argumentos extras para a CLI."""
    from lastro.codex.launcher import LaunchRefused, result_json
    from lastro.codex.launcher import launch as run_codex
    from lastro.codex.lifecycle import summary_lines
    from lastro.codex.models import LaunchMode
    from lastro.codex.preflight import CodexPreflightError

    state = get_state(ctx)
    project = state.require_history_project()
    if not headless and not (sys.stdin.isatty() and sys.stdout.isatty()):
        _usage("Interactive Codex needs a terminal; use --headless without a TTY.")
    store = state.open_history(project)
    from lastro.execution.supervisor import enforce_deadlines

    # Run aguardando launch também tem prazo: expirada, termina antes de abrir o executor.
    with contextlib.suppress(HistoryError):
        enforce_deadlines(store)
    mode = LaunchMode.HEADLESS if headless else LaunchMode.INTERACTIVE
    try:
        result = run_codex(project, store, run_id, mode, write=output.info)
    except (LaunchRefused, CodexPreflightError) as exc:
        _usage(str(exc))
    for line in summary_lines(result):
        output.info(line)
    if headless:
        output.info(result_json(result))
    raise typer.Exit(code=result.exit_code)


def _native_preflight(project: ProjectContext) -> Any:
    """Antes de criar a run: extra, chave (por contagem de tokens), modelos e bundle."""
    from lastro.native.runner import PreflightError, preflight

    try:
        return asyncio.run(preflight(project))
    except PreflightError as exc:
        output.error(str(exc))
        raise typer.Exit(code=USAGE_EXIT_CODE) from None


def _run_native(
    project: ProjectContext, store: HistoryStore, created: CreatedRun, prepared: Any
) -> int:
    from lastro.native.runner import NativeRunner, exit_code, summary_lines

    settings, provider = prepared
    runner = NativeRunner(
        project,
        store,
        created.record.id,
        settings,
        provider,
        write=output.info,
        interactive=sys.stdin.isatty(),
    )
    try:
        outcome = asyncio.run(runner.run())
    except KeyboardInterrupt:
        outcome = runner.outcome
        if outcome is not None:
            for line in summary_lines(outcome):
                output.info(line)
        return INTERRUPTED_EXIT_CODE
    for line in summary_lines(outcome):
        output.info(line)
    return exit_code(outcome)


def print_stop(outcome: Any) -> int:
    """Estado, motivo e relatório efetivos; nunca afirma um relatório que não existe."""
    from lastro.execution.models import stop_message

    path = str(outcome.report_path) if outcome.report_path is not None else None
    if outcome.already_ended:
        reason = f" ({outcome.reason})" if outcome.reason else ""
        output.info(f"Run {outcome.run_id} already ended: {outcome.status}{reason}.")
        if path is not None:
            output.info(f"Report: {path}")
        return 0
    limit = None
    summary = outcome.summary
    if summary is not None and outcome.reason == "action_limit":
        limit = summary.max_actions
    if outcome.report_failed:
        output.info(stop_message(outcome.reason, limit, None))
        output.error("The partial report could not be produced; the run state was saved.")
        return 1
    output.info(stop_message(outcome.reason, limit, path))
    pending = summary.pending_items if summary is not None else ()
    if pending:
        output.info(f"Pending items: {len(pending)} (see the report)")
    return 0


@run_app.command("cancel")
def cancel(
    ctx: typer.Context,
    run_id: Annotated[
        str | None, typer.Argument(help="Run a cancelar; sem id, usa a única run em andamento.")
    ] = None,
) -> None:
    """Cancela a run: trava escritas, retira aprovações pendentes e gera o relatório parcial."""
    from lastro.execution.supervisor import stop_run

    state = get_state(ctx)
    project = state.require_history_project()
    store = state.open_history(project)
    try:
        outcome = stop_run(store, run_id)
    except HistoryError as exc:
        output.error(str(exc))
        raise typer.Exit(code=HISTORY_ERROR_EXIT_CODE) from None
    raise typer.Exit(code=print_stop(outcome))


@run_app.command("resume")
def resume(
    ctx: typer.Context,
    run_id: Annotated[
        str, typer.Argument(help="Run interrompida, cancelada ou parada por limite.")
    ],
) -> None:
    """Cria uma run vinculada com orçamento novo e entrega ao executor registrado na origem.

    Antes de repetir uma edição que pode ter sido enviada, o servidor confere o registro real:
    edições já aplicadas são registradas como already_applied e nunca reenviadas.
    """
    from lastro.execution.resume import ResumeError, create_resumed_run, preflight
    from lastro.execution.supervisor import enforce_deadlines

    state = get_state(ctx)
    project = state.require_history_project()
    store = state.open_history(project)
    with contextlib.suppress(HistoryError):
        enforce_deadlines(store)
    try:
        source = preflight(store, project, run_id)
    except ResumeError as exc:
        output.error(f"{exc} ({exc.code})")
        raise typer.Exit(code=HISTORY_ERROR_EXIT_CODE) from None
    except HistoryError as exc:
        output.error(str(exc))
        raise typer.Exit(code=HISTORY_ERROR_EXIT_CODE) from None
    # Mesmo executor da origem, sujeito à configuração atual; nunca troca implicitamente.
    project = project.with_executor(source.executor_name)
    codex = _codex_preflight(project) if project.config.executor == "codex" else None
    bundle_hash = _bundle_hash(project.loaded.project_dir, project.config.executor)
    embedded = executor_spec(project.config.executor).embedded
    prepared = _native_preflight(project) if embedded else None
    try:
        created = create_resumed_run(
            store,
            project,
            source,
            executor=ExecutorInfo(name="codex", version=codex.install.version) if codex else None,
            skill_bundle_version=bundle_hash,
            executor_snapshot=_codex_snapshot(codex) if codex else None,
        )
    except HistoryError as exc:
        output.error(str(exc))
        raise typer.Exit(code=HISTORY_ERROR_EXIT_CODE) from None
    output.info(f"Run: {created.record.id}")
    output.info(f"Resumed from: {source.id} ({source.status.value}, {source.status_reason})")
    output.info(f"Pasta: {created.paths.run_dir}")
    pending = [item for item in store.pending_items(created.record.id) if item.kind == "write"]
    if pending:
        output.info(
            f"Writes to verify before repeating: {len(pending)} (checked against the real record)"
        )
    if prepared is not None:
        raise typer.Exit(code=_run_native(project, store, created, prepared))
    if codex is not None:
        output.info(f"Executor: codex {codex.install.version}")
        output.info(f"Start Codex with: {_launch_command(state, created.record.id)}")
        return
    handoff(created)


@run_app.command("reverify-tenant")
def reverify_tenant(ctx: typer.Context) -> None:
    """Solicita ao browser dono da run uma nova verificação de tenant."""
    from lastro.browser.control import request_reverification

    state = get_state(ctx)
    project = state.require_history_project()
    history = state.open_history(project)
    run = history.get_active_run()
    if run is None:
        output.error("No active run. Start one with 'lastro run start'.")
        raise typer.Exit(code=USAGE_EXIT_CODE)
    try:
        result = request_reverification(project.project_data_dir, run.id)
    except (OSError, ValueError):
        output.error("Browser owner unavailable; no new browser was started.")
        raise typer.Exit(code=USAGE_EXIT_CODE) from None
    output.info(result)
