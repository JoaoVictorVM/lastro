"""Grupo lastro report: verificação de integridade e exportação pública dos relatórios."""

from typing import Annotated

import typer

from lastro.cli import output
from lastro.cli.state import HISTORY_ERROR_EXIT_CODE, get_state
from lastro.history import HistoryError, HistoryStore, RunNotFoundError
from lastro.reporting.integrity import verify_artifacts
from lastro.reporting.public import export_public

INTEGRITY_FAILED_EXIT_CODE = 2

report_app = typer.Typer(help="Verify and export run reports.", no_args_is_help=True)

RunIdArgument = Annotated[str, typer.Argument(help="Run id shown by 'lastro history'.")]


def _store_for_run(ctx: typer.Context, run_id: str) -> HistoryStore:
    state = get_state(ctx)
    store = state.open_history(state.require_history_config())
    try:
        store.get_run(run_id)
    except RunNotFoundError:
        output.error(f"Run not found in this project: {run_id}")
        raise typer.Exit(code=HISTORY_ERROR_EXIT_CODE) from None
    return store


@report_app.command("verify")
def verify(ctx: typer.Context, run_id: RunIdArgument) -> None:
    """Recompute size and SHA-256 of every artifact of the run; nothing is changed."""
    store = _store_for_run(ctx, run_id)
    result = verify_artifacts(store.run_dir(run_id), store.list_artifacts(run_id))
    if result.valid:
        output.info(f"✓ {result.checked} artifacts of run {run_id} match their recorded hashes.")
        return
    for problem in result.problems:
        output.error(f"✗ {problem.message}")
    output.error(
        f"Integrity check failed: {len(result.problems)} of {result.checked} artifacts "
        f"of run {run_id} do not match the registry."
    )
    raise typer.Exit(code=INTEGRITY_FAILED_EXIT_CODE)


@report_app.command("export")
def export(
    ctx: typer.Context,
    run_id: RunIdArgument,
    public: Annotated[
        bool,
        typer.Option(
            "--public",
            help="Write report.public.md without tenant, URLs, record ids or screenshots, "
            "unless the run profile is marked public.",
        ),
    ] = False,
) -> None:
    """Export a shareable copy of the run report."""
    if not public:
        output.error("Only the public export is available; pass --public.")
        raise typer.Exit(code=HISTORY_ERROR_EXIT_CODE)
    store = _store_for_run(ctx, run_id)
    try:
        exported = export_public(store, run_id)
    except HistoryError as exc:
        output.error(str(exc))
        raise typer.Exit(code=HISTORY_ERROR_EXIT_CODE) from None
    if exported.public_profile:
        output.info("Profile is marked public; the export keeps the original report content.")
    output.info(str(exported.path))
