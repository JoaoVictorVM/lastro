"""Supervisão fora do browser owner: cancelamento pela CLI, término sem owner e prazo de launch.

Sem owner vivo, nenhuma escrita pode sair (o guard consulta o estado durável), então encerrar
localmente é seguro. Com owner vivo, ele finaliza; o supervisor só assume depois do deadline.
"""

import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from lastro.execution.models import (
    DURATION_LIMIT,
    FINALIZE_SECONDS,
    USER_CANCELLED,
    ExecutionSummary,
)
from lastro.history import (
    ApprovalError,
    ApprovalStatus,
    HistoryError,
    HistoryStore,
    RunNotFoundError,
    RunRecord,
    RunStatus,
)
from lastro.timeutil import parse_timestamp, utc_now

POLL_SECONDS = 0.1
# Margem reservada para o término local e o relatório quando o owner não responde a tempo.
LOCAL_FINISH_MARGIN = 4.0


class NoRunToCancelError(HistoryError):
    """Sem id e sem uma única run ativa no projeto."""


@dataclass(frozen=True)
class StopOutcome:
    run_id: str
    status: str
    reason: str | None
    already_ended: bool
    finished_by: str
    report_kind: str | None
    report_path: Path | None
    report_status: str | None
    summary: ExecutionSummary | None = None

    @property
    def report_failed(self) -> bool:
        return self.report_path is None or self.report_status == "failed"


def resolve_run(store: HistoryStore, run_id: str | None) -> RunRecord:
    """Run informada do projeto, ou a única run não terminal quando o id é omitido."""
    if run_id is not None:
        record = store.get_run(run_id)
        if record.project_id != store.project_id:
            raise RunNotFoundError(run_id)
        return record
    active = store.list_non_terminal()
    if len(active) != 1:
        raise NoRunToCancelError(
            "No active run in this project. Pass the run id: lastro run cancel <run-id>"
            if not active
            else "More than one unfinished run; pass the run id: lastro run cancel <run-id>"
        )
    return active[0]


def owner_alive(store: HistoryStore, record: RunRecord) -> bool:
    return store.get_active_run(record.id) is not None


def withdraw_approvals(store: HistoryStore, run_id: str, reason: str) -> None:
    """Retira pedidos abertos e aprovações não usadas; a decisão humana fica registrada."""
    for record in store.list_approvals(run_id):
        with contextlib.suppress(ApprovalError, HistoryError):
            if record.status == ApprovalStatus.PENDING:
                store.decide_approval(
                    record.action_id, "skip", "system", note=reason, run_id=run_id
                )
            elif record.status == ApprovalStatus.APPROVED:
                store.invalidate_approval(run_id, record.action_id, reason)


def existing_report(store: HistoryStore, run_id: str) -> tuple[str | None, Path | None, str | None]:
    artifacts = {item.id: item for item in store.list_artifacts(run_id)}
    for snapshot in reversed(store.list_report_snapshots(run_id)):
        artifact = artifacts.get(snapshot.report_artifact_id or snapshot.fallback_artifact_id or "")
        if artifact is not None:
            return (
                snapshot.kind.value,
                store.run_dir(run_id) / artifact.relative_path,
                snapshot.status.value,
            )
    return None, None, None


def finish_locally(
    store: HistoryStore, run_id: str, status: RunStatus, reason: str
) -> tuple[str | None, Path | None, str | None]:
    """Término confiável sem browser: retira gates, fecha escritas ambíguas, termina e relata."""
    from lastro.reporting.builder import ReportBuilder

    if status == RunStatus.CANCELLED:
        withdraw_approvals(store, run_id, "cancelled")
    with contextlib.suppress(HistoryError):
        store.mark_unknown_writes(run_id)
    record = store.get_run(run_id)
    if not record.status.is_terminal:
        with contextlib.suppress(HistoryError):
            store.seal_criterion_results(run_id)
        with contextlib.suppress(HistoryError):
            record = store.finish_run(run_id, status, reason)
    kind, path, report_status = existing_report(store, run_id)
    if path is not None:
        return kind, path, report_status
    record = store.get_run(run_id)
    try:
        report = ReportBuilder(store).build_partial(run_id, record.status_reason or reason)
    except Exception:
        return "partial", None, "failed"
    return "partial", report.report_path or report.fallback_path, report.status.value


def _outcome(
    store: HistoryStore,
    run_id: str,
    *,
    already_ended: bool,
    finished_by: str,
    report: tuple[str | None, Path | None, str | None],
) -> StopOutcome:
    record = store.get_run(run_id)
    summary: ExecutionSummary | None = None
    with contextlib.suppress(HistoryError):
        summary = store.execution_summary(run_id)
    kind, path, report_status = report
    return StopOutcome(
        run_id=run_id,
        status=record.status.value,
        reason=record.status_reason,
        already_ended=already_ended,
        finished_by=finished_by,
        report_kind=kind,
        report_path=path,
        report_status=report_status,
        summary=summary,
    )


def stop_run(
    store: HistoryStore,
    run_id: str | None,
    *,
    status: RunStatus = RunStatus.CANCELLED,
    reason: str = USER_CANCELLED,
    origin: str = "cli",
    timeout: float = FINALIZE_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> StopOutcome:
    """Grava o pedido durável e espera o término dentro de um único deadline absoluto."""
    record = resolve_run(store, run_id)
    if record.status.is_terminal:
        return _outcome(
            store,
            record.id,
            already_ended=True,
            finished_by="existing",
            report=existing_report(store, record.id),
        )
    deadline = monotonic() + timeout
    latch = store.latch_stop(record.id, status, reason, origin=origin)
    target = RunStatus(latch.state.stop_status or status.value)
    cause = latch.state.stop_reason or reason
    finished_by = "owner"
    while True:
        current = store.get_run(record.id)
        if current.status.is_terminal:
            break
        remaining = deadline - monotonic()
        if not owner_alive(store, current) or remaining <= LOCAL_FINISH_MARGIN:
            finished_by = "supervisor"
            bounded = store.with_retry_budget(max(0.5, remaining))
            return _outcome(
                store,
                record.id,
                already_ended=False,
                finished_by=finished_by,
                report=finish_locally(bounded, record.id, target, cause),
            )
        sleep(POLL_SECONDS)
    # O owner terminou: o relatório sai logo depois do estado terminal.
    report = existing_report(store, record.id)
    while report[1] is None and monotonic() < deadline:
        sleep(POLL_SECONDS)
        report = existing_report(store, record.id)
    if report[1] is None:
        report = finish_locally(store, record.id, target, cause)
    return _outcome(store, record.id, already_ended=False, finished_by=finished_by, report=report)


def enforce_deadlines(store: HistoryStore, now: Callable[[], datetime] = utc_now) -> list[str]:
    """Runs sem owner vivo além do prazo terminam por duração; nenhuma fica sem supervisor.

    Cobre a janela entre a criação e o claim do launcher, quando ainda não há browser owner.
    """
    expired: list[str] = []
    moment = now()
    for record in store.list_non_terminal():
        if owner_alive(store, record):
            continue
        state = store.execution_state(record.id)
        if parse_timestamp(state.deadline_at) > moment:
            continue
        latch = store.latch_stop(record.id, RunStatus.LIMIT_REACHED, DURATION_LIMIT)
        finish_locally(
            store,
            record.id,
            RunStatus(latch.state.stop_status or RunStatus.LIMIT_REACHED.value),
            latch.state.stop_reason or DURATION_LIMIT,
        )
        expired.append(record.id)
    return expired
