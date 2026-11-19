"""Comandos `lastro eval`: execução headless, retomada, consulta, exportação e revisão.

Nenhuma pergunta interativa durante `run` ou `resume`. Com `--json`, o resultado vai para stdout e
o progresso para stderr. Código 0: processamento concluído (inclusive preliminar); 2: uso,
configuração ou preflight; 4: pausa por limite do provedor ou orçamento; 130: interrupção.
"""

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from lastro.cli import output
from lastro.cli.state import HISTORY_ERROR_EXIT_CODE, get_state
from lastro.evaluation.models import USAGE_EXIT_CODE, EvalError
from lastro.history import HistoryError, HistoryStore

eval_app = typer.Typer(
    help="Executa avaliações reproduzíveis do agente contra o demo.", no_args_is_help=True
)
review_app = typer.Typer(help="Revisão humana da amostra do juiz.", no_args_is_help=True)
eval_app.add_typer(review_app, name="review")

type DepsFactory = Callable[..., Any]


def default_deps(
    history: HistoryStore,
    project_dir: Path,
    project_id: str,
    suite_id: str,
    executor_name: str,
    model: str | None,
    judge_name: str,
    judge_model: str | None,
    progress: Callable[[Any], None],
) -> Any:
    """Dependências reais: demo próprio da suite, executor e juiz instalados localmente."""
    import asyncio

    from lastro.evaluation.demo import ManagedDemo
    from lastro.evaluation.executors import default_executor, default_judge, scripted_login
    from lastro.evaluation.runner import RunnerDeps
    from lastro.evaluation.workspace import suite_root
    from lastro_demo.config import load_seed_users

    user = next(item for item in load_seed_users({}) if item.key == "aurora")

    def login(url: str, state: Path) -> None:
        asyncio.run(scripted_login(url, user.email, user.password, "aurora", state))

    return RunnerDeps(
        history=history,
        project_dir=project_dir,
        project_id=project_id,
        demo=ManagedDemo(suite_root(project_dir, suite_id) / "demo", owner_token=suite_id),
        executor=default_executor(executor_name, model, output.error),
        judge=default_judge(judge_name, judge_model),
        credentials=(user.email, user.password),
        progress=progress,
        write=output.error,
        baseline_login=login,
    )


# Ponto de injeção dos testes: executores e demo simulados no lugar dos reais.
DEPS_FACTORY: DepsFactory = default_deps


def default_retrieval_deps(
    history: HistoryStore,
    project_dir: Path,
    project_root: Path,
    private_root: Path,
    executor_name: str,
    model: str | None,
    judge_name: str,
    judge_model: str | None,
    progress: Callable[[Any], None],
) -> Any:
    """Resposta e Ragas por sessões headless sem tools; índice isolado com o modelo local."""
    from lastro.evaluation.executors import default_answerer, default_judge
    from lastro.evaluation.ragas_adapter import RagasAdapter
    from lastro.evaluation.retrieval import RetrievalDeps, default_service

    return RetrievalDeps(
        history=history,
        project_dir=project_dir,
        project_root=project_root,
        answerer=default_answerer(executor_name, model),
        judge=default_judge(judge_name, judge_model),
        ragas=RagasAdapter(),
        service_factory=default_service(private_root),
        progress=progress,
    )


RETRIEVAL_DEPS_FACTORY: DepsFactory = default_retrieval_deps


def _fail(exc: EvalError) -> typer.Exit:
    output.error(f"{exc.code}: {exc.message}")
    code = USAGE_EXIT_CODE
    if exc.code in {"lease_busy", "owner_unverified", "lease_lost"}:
        code = HISTORY_ERROR_EXIT_CODE
    return typer.Exit(code=code)


def _eta(seconds: float | None) -> str:
    if seconds is None:
        return "ETA unavailable"
    minutes, rest = divmod(int(seconds), 60)
    return f"ETA {minutes}m{rest:02d}s"


def _progress(progress: Any) -> None:
    line = (
        f"run {progress.done}/{progress.total} · condition {progress.condition} · "
        f"run {progress.repetition}/{progress.runs_per_condition} · {_eta(progress.eta_seconds)}"
    )
    print(output.to_console(line, sys.stderr), file=sys.stderr, flush=True)


def _summary(history: HistoryStore, suite_id: str, public_report: Path | None) -> dict[str, Any]:
    from lastro.evaluation.review import review_status

    repo = history.evaluations
    suite = repo.get_suite(suite_id)
    trials = [item for item in repo.list_trials(suite_id) if item.role == "measured"]
    review = review_status(history, suite_id)
    limitations: list[str] = []
    if not review.complete:
        limitations.append("manual_review_pending")
    results = repo.list_results(suite_id) if suite.current_revision else []
    if any(item.metric == "cost_usd" and item.n < item.planned_n for item in results):
        limitations.append("external_cost_unavailable")
    return {
        "schema_version": 1,
        "suite_run_id": suite.id,
        "suite": suite.suite_name,
        "state": suite.status,
        "pause_reason": suite.pause_reason,
        "executor": {"name": suite.executor_name, "version": suite.executor_version},
        "trials": {
            "planned": sum(1 for item in trials if item.status != "not_applicable"),
            "valid": sum(1 for item in trials if item.status == "valid"),
            "invalid": sum(1 for item in trials if item.status == "invalid"),
            "paused": sum(1 for item in trials if item.status == "paused"),
            "pending": sum(1 for item in trials if item.status == "pending"),
            "not_applicable": sum(1 for item in trials if item.status == "not_applicable"),
        },
        "review": {
            "sample_required": review.sample_required,
            "sample_reviewed": review.sample_reviewed,
            "agreement": review.agreement,
        },
        "results_revision": suite.current_revision,
        "public_report": str(public_report) if public_report else None,
        "limitations": limitations,
    }


def _report(history: HistoryStore, suite_id: str, as_json: bool, path: Path | None) -> None:
    from lastro.evaluation.export import collect, table

    summary = _summary(history, suite_id, path)
    if as_json:
        typer.echo(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return
    output.info(f"Suite run {summary['suite_run_id']}: {summary['state']}")
    trials = summary["trials"]
    output.info(
        f"Trials: {trials['valid']} valid, {trials['invalid']} invalid, {trials['paused']} "
        f"paused, {trials['pending']} pending of {trials['planned']} planned"
    )
    for line in table(collect(history, suite_id)):
        output.info(line)
    review = summary["review"]
    output.info(
        f"Review: {review['sample_reviewed']}/{review['sample_required']} sampled findings "
        f"reviewed; agreement {review['agreement'] if review['agreement'] is not None else 'n/a'}"
    )
    for item in summary["limitations"]:
        output.info(f"Limitation: {item}")
    if path is not None:
        output.info(f"Results: {path}")


def _open(ctx: typer.Context) -> tuple[HistoryStore, Path, str, str, Path]:
    state = get_state(ctx)
    project = state.require_history_project()
    history = state.open_history(project)
    return (
        history,
        history.project_dir,
        project.require_project_id(),
        project.timezone,
        project.loaded.project_dir,
    )


def _execute(
    ctx: typer.Context, suite_id: str, *, resume: bool, as_json: bool, executor: str | None = None
) -> None:
    from lastro.evaluation.export import export
    from lastro.evaluation.retrieval import is_retrieval
    from lastro.evaluation.runner import SuiteRunner
    from lastro.evaluation.suites import spec_from_manifest

    history, project_dir, project_id, timezone, root = _open(ctx)
    try:
        suite = history.evaluations.get_suite(suite_id)
    except HistoryError as exc:
        output.error(str(exc))
        raise typer.Exit(code=USAGE_EXIT_CODE) from None
    if is_retrieval(suite):
        _execute_retrieval(ctx, history, suite_id, resume=resume, as_json=as_json)
        return
    spec = spec_from_manifest(suite.manifest)
    judge_name = spec.judge.executor or suite.executor_name
    try:
        deps = DEPS_FACTORY(
            history,
            project_dir,
            project_id,
            suite_id,
            executor or suite.executor_name,
            spec.model,
            judge_name,
            spec.judge.model,
            _progress,
        )
        outcome = SuiteRunner(deps).run(suite_id, resume=resume)
    except EvalError as exc:
        raise _fail(exc) from None
    except HistoryError as exc:
        code = getattr(exc, "code", None)
        if code in {"lease_busy", "owner_unverified"}:
            output.error(str(exc))
            raise typer.Exit(code=HISTORY_ERROR_EXIT_CODE) from None
        output.error(str(exc))
        raise typer.Exit(code=HISTORY_ERROR_EXIT_CODE) from None
    path: Path | None = None
    try:
        path = export(history, suite_id, public=True, root=root, timezone=timezone)
    except OSError as exc:
        output.error(f"export_failed: {exc}; results stay in the history")
    _report(history, suite_id, as_json, path)
    if outcome.exit_code:
        if outcome.exit_code == 4:
            output.error(
                f"Suite paused ({outcome.message}); continue with 'lastro eval resume {suite_id}'."
            )
        raise typer.Exit(code=outcome.exit_code)


def _retrieval_deps(
    ctx: typer.Context,
    history: HistoryStore,
    executor: str,
    model: str | None,
    judge: str,
    judge_model: str | None,
) -> Any:
    project = get_state(ctx).require_history_project()
    return RETRIEVAL_DEPS_FACTORY(
        history,
        history.project_dir,
        project.loaded.project_dir,
        project.private_data_root,
        executor,
        model,
        judge,
        judge_model,
        _progress,
    )


def _execute_retrieval(
    ctx: typer.Context, history: HistoryStore, suite_id: str, *, resume: bool, as_json: bool
) -> None:
    from lastro.evaluation.export import export
    from lastro.evaluation.retrieval import RetrievalRunner, suite_spec

    _, _, _, timezone, root = _open(ctx)
    suite = history.evaluations.get_suite(suite_id)
    spec = suite_spec(suite.manifest)
    try:
        deps = _retrieval_deps(
            ctx,
            history,
            suite.executor_name,
            spec.model,
            spec.judge.executor or suite.executor_name,
            spec.judge.model,
        )
        outcome = RetrievalRunner(deps).run(suite_id, resume=resume)
    except EvalError as exc:
        raise _fail(exc) from None
    except HistoryError as exc:
        output.error(str(exc))
        raise typer.Exit(code=HISTORY_ERROR_EXIT_CODE) from None
    path: Path | None = None
    try:
        path = export(history, suite_id, public=True, root=root, timezone=timezone)
    except OSError as exc:
        output.error(f"export_failed: {exc}; results stay in the history")
    _report(history, suite_id, as_json, path)
    if outcome.exit_code:
        if outcome.exit_code == 4:
            output.error(
                f"Suite paused ({outcome.message}); continue with 'lastro eval resume {suite_id}'."
            )
        raise typer.Exit(code=outcome.exit_code)


def _run_retrieval(ctx: typer.Context, suite: str, executor: str | None, as_json: bool) -> None:
    from lastro.evaluation.retrieval import create_retrieval_suite, load_retrieval_suite

    history, _, _, _, root = _open(ctx)
    try:
        loaded = load_retrieval_suite(suite, root, cwd=Path.cwd())
        name = executor or loaded.spec.executor
        judge_name = loaded.spec.judge.executor or name
        deps = _retrieval_deps(
            ctx, history, name, loaded.spec.model, judge_name, loaded.spec.judge.model
        )
        record = create_retrieval_suite(
            history.evaluations,
            loaded,
            deps.answerer.identity().as_json(),
            deps.judge.identity().as_json(),
            deps.ragas.identity(),
        )
    except EvalError as exc:
        raise _fail(exc) from None
    message = f"Suite run {record.id} ({loaded.spec.id}, retrieval, executor {name})"
    if as_json:
        print(message, file=sys.stderr)
    else:
        output.info(message)
    _execute_retrieval(ctx, history, record.id, resume=False, as_json=as_json)


@eval_app.command("run")
def run(
    ctx: typer.Context,
    suite: Annotated[str, typer.Argument(help="Nome distribuído (demo-core) ou caminho YAML.")],
    executor: Annotated[
        str | None, typer.Option("--executor", help="claude-code, codex ou native.")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Resultado JSON em stdout.")] = False,
) -> None:
    """Valida tudo antes de abrir sessões, congela a suite e executa sem interação."""
    from lastro.evaluation.models import EXECUTORS
    from lastro.evaluation.retrieval import PURPOSE, peek_purpose
    from lastro.evaluation.runner import create_suite
    from lastro.evaluation.suites import load_suite

    if executor is not None and executor not in EXECUTORS:
        output.error(f"invalid_suite: unknown executor {executor}")
        raise typer.Exit(code=USAGE_EXIT_CODE)
    if peek_purpose(suite, Path.cwd()) == PURPOSE:
        _run_retrieval(ctx, suite, executor, as_json)
        return
    history, project_dir, project_id, _timezone, _root = _open(ctx)
    try:
        loaded = load_suite(suite, cwd=Path.cwd())
        name = executor or loaded.spec.executor
        judge_name = loaded.spec.judge.executor or name
        deps = DEPS_FACTORY(
            history,
            project_dir,
            project_id,
            "preflight",
            name,
            loaded.spec.model,
            judge_name,
            loaded.spec.judge.model,
            _progress,
        )
        identity = deps.executor.identity()
        if deps.judge is not None:
            deps.judge.identity()
        record = create_suite(history.evaluations, loaded, name, identity.version)
    except EvalError as exc:
        raise _fail(exc) from None
    message = f"Suite run {record.id} ({loaded.spec.id}, executor {name})"
    if as_json:
        print(message, file=sys.stderr)
    else:
        output.info(message)
    _execute(ctx, record.id, resume=False, as_json=as_json, executor=name)


@eval_app.command("resume")
def resume(
    ctx: typer.Context,
    suite_run_id: Annotated[str, typer.Argument(help="Id impresso por lastro eval run.")],
    as_json: Annotated[bool, typer.Option("--json", help="Resultado JSON em stdout.")] = False,
) -> None:
    """Continua slots pendentes ou o julgamento, sem mudar a configuração congelada."""
    _execute(ctx, suite_run_id, resume=True, as_json=as_json)


@eval_app.command("show")
def show(
    ctx: typer.Context,
    suite_run_id: Annotated[str, typer.Argument(help="Id da suite-run.")],
    as_json: Annotated[bool, typer.Option("--json", help="Resultado JSON em stdout.")] = False,
) -> None:
    """Estado, contagens, revisão e métricas disponíveis."""
    history, *_ = _open(ctx)
    try:
        history.evaluations.get_suite(suite_run_id)
    except HistoryError as exc:
        output.error(str(exc))
        raise typer.Exit(code=USAGE_EXIT_CODE) from None
    _report(history, suite_run_id, as_json, None)


@eval_app.command("export")
def export_cmd(
    ctx: typer.Context,
    suite_run_id: Annotated[str, typer.Argument(help="Id da suite-run.")],
    public: Annotated[
        bool, typer.Option("--public", help="Relatório sanitizado em evals/results/public.")
    ] = False,
) -> None:
    """Markdown privado (padrão) ou público sanitizado; reexportar é idempotente."""
    from lastro.evaluation.export import export

    history, _, _, timezone, root = _open(ctx)
    try:
        history.evaluations.get_suite(suite_run_id)
        path = export(history, suite_run_id, public=public, root=root, timezone=timezone)
    except HistoryError as exc:
        output.error(str(exc))
        raise typer.Exit(code=USAGE_EXIT_CODE) from None
    except OSError as exc:
        output.error(f"export_failed: {exc}")
        raise typer.Exit(code=HISTORY_ERROR_EXIT_CODE) from None
    output.info(f"Exported: {path}")


@review_app.command("export")
def review_export(
    ctx: typer.Context,
    suite_run_id: Annotated[str, typer.Argument(help="Id da suite-run.")],
) -> None:
    """JSONL cego da amostra e da fila adicional; o manifesto fica privado."""
    from lastro.evaluation.review import export_review

    history, project_dir, *_ = _open(ctx)
    try:
        path = export_review(history, suite_run_id, project_dir)
    except EvalError as exc:
        raise _fail(exc) from None
    except HistoryError as exc:
        output.error(str(exc))
        raise typer.Exit(code=USAGE_EXIT_CODE) from None
    output.info(f"Review file: {path}")
    output.info("Fill decision, bug_id and reason, then run 'lastro eval review import'.")


@review_app.command("import")
def review_import(
    ctx: typer.Context,
    suite_run_id: Annotated[str, typer.Argument(help="Id da suite-run.")],
    file: Annotated[Path, typer.Option("--file", help="JSONL preenchido.")],
    reviewer: Annotated[str, typer.Option("--reviewer", help="Identidade local do revisor.")],
) -> None:
    """Importa revisões append-only, recalcula concordância e gera novo agregado."""
    from lastro.evaluation.metrics import recompute
    from lastro.evaluation.models import SuiteStatus
    from lastro.evaluation.review import import_reviews, review_status

    history, project_dir, *_ = _open(ctx)
    try:
        created, replayed = import_reviews(history, suite_run_id, file, reviewer)
        revision = recompute(history, suite_run_id, project_dir)
        status = review_status(history, suite_run_id)
        suite = history.evaluations.get_suite(suite_run_id)
        if status.complete and suite.status == SuiteStatus.AWAITING_REVIEW.value:
            history.evaluations.set_suite_status(None, suite_run_id, SuiteStatus.COMPLETED.value)
    except EvalError as exc:
        raise _fail(exc) from None
    except HistoryError as exc:
        output.error(str(exc))
        raise typer.Exit(code=USAGE_EXIT_CODE) from None
    agreement = "n/a" if status.agreement is None else f"{status.agreement * 100:.1f}%"
    output.info(
        f"Imported {created} reviews ({replayed} unchanged); results revision {revision}; "
        f"agreement {agreement} ({status.agreement_numerator}/{status.agreement_denominator})"
    )
