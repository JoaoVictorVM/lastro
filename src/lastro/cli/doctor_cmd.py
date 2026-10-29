"""Comando lastro doctor: uma linha por check e código de saída 0, 1 ou 2."""

from collections.abc import Callable, Sequence

import typer

from lastro.cli import output
from lastro.cli.output import Status
from lastro.cli.state import get_state
from lastro.doctor.checks import CheckResult, DoctorEnvironment
from lastro.doctor.runner import default_environment, exit_code_for, run_doctor

DETAIL_INDENT = "    "

environment_factory: Callable[[], DoctorEnvironment] = default_environment


def render(results: Sequence[CheckResult], unicode: bool | None = None) -> list[str]:
    lines: list[str] = []
    for result in results:
        first, *rest = result.detail.split("\n")
        lines.append(f"{output.icon(result.status, unicode)} {result.name}: {first}")
        lines.extend(f"{DETAIL_INDENT}{extra}" for extra in rest)
        if result.status is not Status.OK and result.fix:
            lines.append(f"{DETAIL_INDENT}Correção: {result.fix}")
    return lines


def doctor(ctx: typer.Context) -> None:
    """Verifica Python, navegador, executor, configuração, credenciais e o private data root."""
    state = get_state(ctx)
    results = run_doctor(environment_factory(), state.config_path, state.profile)
    for line in render(results):
        output.info(line)
    raise typer.Exit(code=exit_code_for(results))
