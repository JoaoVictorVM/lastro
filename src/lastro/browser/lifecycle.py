"""Finalização comum no browser owner: término normal do agente e encerramento confiável."""

import asyncio
import contextlib
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from lastro.execution.models import FINALIZE_SECONDS, USER_CANCELLED
from lastro.history import HistoryError, HistoryStore, RunRecord, RunStatus
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


async def _bounded(awaitable: Any, until: float, monotonic: Callable[[], float]) -> bool:
    """Espera limitada ao deadline absoluto; esgotado, a tarefa local é cancelada."""
    task = asyncio.ensure_future(awaitable)
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=max(0.0, until - monotonic()))
    except TimeoutError:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        return False
    return True


async def _close_resources(
    runtime: "BrowserTools", drain_until: float, monotonic: Callable[[], float]
) -> str | None:
    """Respostas e leituras em andamento terminam até o fim da drenagem; depois, fecha tudo."""
    capture_error: str | None = None
    try:
        if not await _bounded(runtime.video.close(), drain_until, monotonic):
            capture_error = "evidence video did not close before the drain deadline"
    except Exception as error:
        capture_error = runtime.session.sanitize(str(error))
    session = runtime.session
    if not await _bounded(session.close(), drain_until, monotonic):
        for task in tuple(session.response_tasks):
            task.cancel()
        with contextlib.suppress(Exception):
            if session.browser is not None:
                await asyncio.wait_for(session.browser.close(), timeout=2.0)
        with contextlib.suppress(Exception):
            if session.playwright is not None:
                await asyncio.wait_for(session.playwright.stop(), timeout=2.0)
        session.context = session.page = session.browser = None
    return capture_error


def withdraw_approvals(runtime: "BrowserTools", status: RunStatus, skip_reason: str) -> None:
    """Cancelamento retira pedidos e aprovações não consumidas; a decisão humana permanece."""
    with contextlib.suppress(Exception):
        if status == RunStatus.CANCELLED and skip_reason == USER_CANCELLED:
            runtime.approvals.withdraw("cancelled")
        else:
            runtime.approvals.skip_pending(skip_reason)


async def finalize(
    runtime: "BrowserTools",
    status: RunStatus,
    reason: str,
    *,
    skip_reason: str,
    drain_until: float | None = None,
    final_until: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Fecha a admissão, drena captura e browser, sela critérios, termina e gera o relatório.

    Uma run já terminal por outro processo não é reaberta: o estado existente é respeitado.
    Com deadline, drenagem e persistência compartilham o mesmo prazo absoluto.
    """
    runtime.session.policy.locks.add("run_ended")
    start = monotonic()
    final_until = final_until if final_until is not None else start + FINALIZE_SECONDS * 4
    drain_until = drain_until if drain_until is not None else final_until
    withdraw_approvals(runtime, status, skip_reason)
    runtime.approvals.close()
    capture_error = await _close_resources(runtime, drain_until, monotonic)
    # Persistência com espera de lock limitada ao tempo restante, sem mudar a política comum.
    history = runtime.history.with_retry_budget(max(0.5, final_until - monotonic()))
    with contextlib.suppress(HistoryError):
        history.mark_unknown_writes(runtime.run_id)
    record = history.get_run(runtime.run_id)
    already_ended = record.status.is_terminal
    if not already_ended:
        with contextlib.suppress(HistoryError):
            history.seal_criterion_results(runtime.run_id)
        record = history.finish_run(runtime.run_id, status, reason)
    result: dict[str, Any] = {"status": record.status.value}
    if already_ended:
        result["already_ended"] = True
        result["status_reason"] = record.status_reason
    if capture_error is not None:
        result["capture_error"] = capture_error
    memory = history.memory_outcome(runtime.run_id)
    if memory is not None:
        result.update(memory_outcome=memory.outcome.value, memory_pending=memory.pending_count)
    # O relatório fica fora da transação terminal: falhar aqui nunca reabre nem muda a run.
    builder = ReportBuilder(history)
    partial = record.status in PARTIAL_STATUSES
    terminal_reason = record.status_reason or reason

    def build() -> Any:
        if partial:
            return builder.build_partial(runtime.run_id, terminal_reason)
        return builder.build_final(runtime.run_id)

    try:
        report = await asyncio.wait_for(
            asyncio.to_thread(build), timeout=max(0.5, final_until - monotonic())
        )
    except TimeoutError:
        result.update(report_status="failed", report_error="report deadline exceeded")
        return result
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
    if not already_ended:
        _detach_reproduction(runtime, history, record)
    return result


def _detach_reproduction(runtime: "BrowserTools", history: HistoryStore, record: RunRecord) -> None:
    """claude-code não tem processo de CLI depois do fim: o owner inicia a reprodução."""
    from lastro.reproduction.trigger import detach_after_owner

    with contextlib.suppress(Exception):
        if history.eligible_findings(record.id):
            detach_after_owner(runtime.project, record)
