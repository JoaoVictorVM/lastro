"""Finalização comum no browser owner: término normal do agente e encerramento confiável."""

import contextlib
from typing import TYPE_CHECKING, Any

from lastro.history import RunStatus
from lastro.reporting.builder import ReportBuilder

if TYPE_CHECKING:
    from lastro.browser.tools import BrowserTools

# Motivos aceitos pelo canal confiável; nunca vêm do modelo.
TRUSTED_REASONS = frozenset(
    {
        "mission_completed",
        "cost_cap",
        "budget_accounting_violation",
        "api_error",
        "api_auth",
        "agent_protocol_error",
        "tool_transport_error",
        "context_limit",
        "graph_limit",
        "executor_configuration",
        "user_cancelled",
        "history_unavailable",
        "executor_limit",
        "executor_rate_limit",
        "executor_network",
        "executor_error",
        "executor_protocol_error",
        "executor_exited",
        "mcp_unavailable",
    }
)
TRUSTED_STATUSES = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.BLOCKED,
        RunStatus.INTERRUPTED,
        RunStatus.CANCELLED,
        RunStatus.LIMIT_REACHED,
    }
)
# Interrupções e limites geram relatório parcial; término declarado gera o final.
PARTIAL_STATUSES = frozenset({RunStatus.INTERRUPTED, RunStatus.CANCELLED, RunStatus.LIMIT_REACHED})


async def finalize(
    runtime: "BrowserTools", status: RunStatus, reason: str, *, skip_reason: str
) -> dict[str, Any]:
    """Fecha a admissão, drena captura e browser, sela critérios, termina e gera o relatório.

    Uma run já terminal por outro processo não é reaberta: o estado existente é respeitado.
    """
    runtime.session.policy.locks.add("run_ended")
    with contextlib.suppress(Exception):
        runtime.approvals.skip_pending(skip_reason)
    runtime.approvals.close()
    capture_error: str | None = None
    try:
        await runtime.video.close()
    except Exception as error:
        capture_error = runtime.session.sanitize(str(error))
    finally:
        await runtime.session.close()
    record = runtime.history.get_run(runtime.run_id)
    already_ended = record.status.is_terminal
    if not already_ended:
        runtime.history.seal_criterion_results(runtime.run_id)
        record = runtime.history.finish_run(runtime.run_id, status, reason)
    result: dict[str, Any] = {"status": record.status.value}
    if already_ended:
        result["already_ended"] = True
        result["status_reason"] = record.status_reason
    if capture_error is not None:
        result["capture_error"] = capture_error
    memory = runtime.history.memory_outcome(runtime.run_id)
    if memory is not None:
        result.update(memory_outcome=memory.outcome.value, memory_pending=memory.pending_count)
    # O relatório fica fora da transação terminal: falhar aqui nunca reabre nem muda a run.
    builder = ReportBuilder(runtime.history)
    try:
        if record.status in PARTIAL_STATUSES:
            report = builder.build_partial(runtime.run_id, record.status_reason or reason)
        else:
            report = builder.build_final(runtime.run_id)
    except Exception as error:
        result.update(report_status="failed", report_error=runtime.session.sanitize(str(error)))
        return result
    result.update(
        report_status=report.status.value,
        report_kind="partial" if record.status in PARTIAL_STATUSES else "final",
        report_path=str(report.report_path or report.fallback_path or ""),
        issue_paths=[str(path) for path in report.issue_paths],
        summary=list(report.summary_lines),
    )
    if report.snapshot.error_text:
        result["report_error"] = report.snapshot.error_text
    return result
