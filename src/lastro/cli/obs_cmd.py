"""Grupo lastro obs: stack Langfuse local, estado da exportação e recuperação explícita."""

import json
from typing import Annotated

import typer

from lastro.cli import output
from lastro.cli.state import CONFIG_ERROR_EXIT_CODE, HISTORY_ERROR_EXIT_CODE, get_state
from lastro.config.loader import ProjectContext
from lastro.history import HistoryError, HistoryStore, RunNotFoundError, RunRecord
from lastro.observability.models import ObservabilityStatus

obs_app = typer.Typer(help="Local Langfuse stack and trace export.", no_args_is_help=True)

JsonOption = Annotated[bool, typer.Option("--json", help="Print the local state as JSON.")]


def _print_status(status: ObservabilityStatus, as_json: bool) -> None:
    if as_json:
        output.info(json.dumps(status.as_json(), ensure_ascii=False))
        return
    output.info(f"Run: {status.run_id}")
    output.info(f"Trace: {status.trace_id}")
    output.info(f"Link: {status.trace_url}")
    output.info(f"Export: {status.export_status}")
    output.info(
        f"Spans: {status.projected_events} projected, {status.acknowledged_spans} acknowledged, "
        f"{status.pending_spans} pending, {status.rejected_spans} rejected"
    )
    if status.waiting_end:
        output.info("The root span is exported when the run ends.")
    if status.last_error_code is not None:
        output.info(f"Last error: {status.last_error_code}")


def _run_for(
    ctx: typer.Context, run_id: str | None
) -> tuple[ProjectContext, HistoryStore, RunRecord]:
    state = get_state(ctx)
    project = state.require_history_project()
    store = state.open_history(project)
    try:
        record = store.get_run(run_id) if run_id is not None else store.get_active_run()
    except RunNotFoundError:
        output.error(f"Run not found in this project: {run_id}")
        raise typer.Exit(code=HISTORY_ERROR_EXIT_CODE) from None
    if record is None:
        output.error("No active run; pass a run id.")
        raise typer.Exit(code=CONFIG_ERROR_EXIT_CODE)
    if record.project_id != project.require_project_id() or record.profile != project.profile_name:
        output.error(f"Run {record.id} belongs to another profile ({record.profile}).")
        raise typer.Exit(code=CONFIG_ERROR_EXIT_CODE)
    return project, store, record


def _require_opt_in(store: HistoryStore, record: RunRecord) -> ObservabilityStatus:
    status = store.observability_status(record.id)
    if status is None:
        output.error(
            f"Run {record.id} has no observability opt-in; nothing is exported (obs_disabled)."
        )
        raise typer.Exit(code=CONFIG_ERROR_EXIT_CODE)
    return status


@obs_app.command("status")
def status(
    ctx: typer.Context,
    run_id: Annotated[
        str | None, typer.Argument(help="Run id; defaults to the active run of the profile.")
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """Local export state of a run; no network, SDK or Docker involved."""
    _, store, record = _run_for(ctx, run_id)
    _print_status(_require_opt_in(store, record), as_json)


@obs_app.command("export")
def export(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run id shown by 'lastro history'.")],
    as_json: JsonOption = False,
) -> None:
    """Retry the pending spans of a run once (up to 5 seconds); the target is never touched."""
    from lastro.observability import ObservabilityError, preflight, settings_source
    from lastro.observability.worker import ExportWorker

    project, store, record = _run_for(ctx, run_id)
    _require_opt_in(store, record)
    try:
        preflight(project.profile.observability)
    except ObservabilityError as exc:
        output.error(f"{exc} ({exc.code})")
        raise typer.Exit(code=CONFIG_ERROR_EXIT_CODE) from None
    try:
        store.unblock_observability(record.id)
        ExportWorker(store, record.id, settings_source(project)).drain(force=True)
        final = store.observability_status(record.id)
    except HistoryError as exc:
        output.error(str(exc))
        raise typer.Exit(code=HISTORY_ERROR_EXIT_CODE) from None
    assert final is not None
    _print_status(final, as_json)
    sendable = final.pending_spans - final.waiting_end
    if final.rejected_spans or sendable or final.export_status in ("blocked", "suspended"):
        raise typer.Exit(code=HISTORY_ERROR_EXIT_CODE)


@obs_app.command("up")
def up(ctx: typer.Context) -> None:
    """Start the local Langfuse stack of this project and print its URL."""
    from lastro.observability.local import LocalStackError, LocalStackManager, stack_for

    state = get_state(ctx)
    project = state.require_history_project()
    manager = LocalStackManager(
        stack_for(project.project_data_dir, project.require_project_id()), progress=output.info
    )
    try:
        url = manager.up()
    except LocalStackError as exc:
        output.error(f"{exc} ({exc.code})")
        raise typer.Exit(code=HISTORY_ERROR_EXIT_CODE) from None
    output.info(f"Langfuse is ready: {url}")
    output.info(f"Stack files: {manager.stack.directory}")
    output.info("Next steps:")
    output.info("  1. Open the URL, sign up and create an organization and a project.")
    output.info("  2. In the project settings, create an API key pair.")
    output.info("  3. Put the keys in .env as LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY.")
    output.info(
        "  4. Fill profiles.<name>.observability in lastro.toml (enabled, endpoint "
        f"{url}/api/public/otel/v1/traces, ui_url {url}, langfuse_project_id)."
    )
    output.info("Observability stays disabled until you enable it in the profile.")
