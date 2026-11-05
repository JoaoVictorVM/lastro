"""Encerramento único da run: owner confiável com lock, ou local depois de o filho morrer."""

import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lastro.browser.control import control_directory, request_termination
from lastro.codex.events import REASON_LIMIT
from lastro.codex.models import CodexResult
from lastro.config.loader import ProjectContext
from lastro.history import HistoryError, HistoryStore, ReportKind, RunRecord, RunStatus

TERMINATION_TIMEOUT_SECONDS = 60.0
OWNER_EXIT_TIMEOUT_SECONDS = 15.0
EXIT_COMPLETED = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_LIMIT = 3
EXIT_CANCELLED = 130
UNKNOWN_USAGE = "Usage and cost: unknown (subscription session; not counted as zero)"

type Writer = Callable[[str], None]


def exit_code_for(status: str, reason: str | None, report_failed: bool = False) -> int:
    """Código do launcher; sucesso do processo Codex nunca entra nesta decisão."""
    if status == RunStatus.CANCELLED.value:
        return EXIT_CANCELLED
    if status == RunStatus.INTERRUPTED.value and reason == REASON_LIMIT:
        return EXIT_LIMIT
    if status == RunStatus.COMPLETED.value and not report_failed:
        return EXIT_COMPLETED
    return EXIT_FAILED


@dataclass(frozen=True)
class Closure:
    report_kind: str | None
    report_path: Path | None
    report_status: str | None
    diagnostic: str | None = None


class RunCloser:
    def __init__(
        self,
        project: ProjectContext,
        history: HistoryStore,
        run_id: str,
        *,
        write: Writer,
        termination_timeout: float = TERMINATION_TIMEOUT_SECONDS,
        owner_exit_timeout: float = OWNER_EXIT_TIMEOUT_SECONDS,
    ) -> None:
        self.project, self.history, self.run_id = project, history, run_id
        self.write = write
        self.termination_timeout = termination_timeout
        self.owner_exit_timeout = owner_exit_timeout

    def _owner_descriptor(self) -> Path:
        return control_directory(self.project.project_data_dir, self.run_id) / "owner.json"

    def request_owner_end(self, status: RunStatus, reason: str) -> Closure | None:
        """Com o MCP vivo: o owner trava efeitos, finaliza e gera o relatório sem inferência."""
        if not self._owner_descriptor().exists():
            return None
        try:
            response = request_termination(
                self.project.project_data_dir,
                self.run_id,
                status.value,
                reason,
                self.termination_timeout,
            )
        except (OSError, ValueError):
            self.write("! browser owner unavailable; closing it before finishing locally.")
            return None
        path = response.get("report_path")
        kind = response.get("report_kind")
        report_status = response.get("report_status")
        report_error = response.get("report_error")
        return Closure(
            report_kind=kind if isinstance(kind, str) else None,
            report_path=Path(path) if isinstance(path, str) else None,
            report_status=report_status if isinstance(report_status, str) else None,
            diagnostic=report_error if isinstance(report_error, str) else None,
        )

    def wait_owner_gone(self) -> bool:
        """Depois da morte do filho: não finalizar enquanto um browser ainda possa escrever."""
        deadline = time.monotonic() + self.owner_exit_timeout
        while time.monotonic() < deadline:
            if not self._owner_descriptor().exists():
                return True
            time.sleep(0.1)
        return False

    def clear_owner_files(self) -> None:
        """Owner encerrado à força não limpa o próprio segredo de controle."""
        directory = self._owner_descriptor().parent
        for name in ("owner.json", "request.json", "response.json"):
            with contextlib.suppress(OSError):
                (directory / name).unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            directory.rmdir()

    def finish_locally(self, status: RunStatus, reason: str) -> Closure:
        """Sem owner: a fachada aceita um único término; concorrência preserva o primeiro."""
        record = self.history.get_run(self.run_id)
        if not record.status.is_terminal:
            with contextlib.suppress(HistoryError):
                self.history.seal_criterion_results(self.run_id)
            with contextlib.suppress(HistoryError):
                self.history.finish_run(self.run_id, status, reason)
        return self.report_for_terminal()

    def report_for_terminal(self) -> Closure:
        """Reaproveita o relatório já gerado; partial só é criado sem nova inferência."""
        record = self.history.get_run(self.run_id)
        existing = self._existing_report()
        if existing is not None:
            return existing
        if record.status not in (
            RunStatus.CANCELLED,
            RunStatus.INTERRUPTED,
            RunStatus.LIMIT_REACHED,
        ):
            return Closure(None, None, None, "no report was generated for this run")
        from lastro.reporting.builder import ReportBuilder

        try:
            report = ReportBuilder(self.history).build_partial(
                self.run_id, record.status_reason or record.status.value
            )
        except Exception as error:
            return Closure(None, None, "failed", f"report failed: {type(error).__name__}")
        return Closure(
            ReportKind.PARTIAL.value,
            report.report_path or report.fallback_path,
            report.status.value,
        )

    def _existing_report(self) -> Closure | None:
        artifacts = {item.id: item for item in self.history.list_artifacts(self.run_id)}
        for snapshot in reversed(self.history.list_report_snapshots(self.run_id)):
            artifact = artifacts.get(
                snapshot.report_artifact_id or snapshot.fallback_artifact_id or ""
            )
            if artifact is not None:
                return Closure(
                    snapshot.kind.value,
                    self.history.run_dir(self.run_id) / artifact.relative_path,
                    snapshot.status.value,
                )
        return None

    def result(
        self, closure: Closure, version: str, codex_exit_code: int | None = None
    ) -> CodexResult:
        record: RunRecord = self.history.get_run(self.run_id)
        failed = closure.report_status == "failed" or closure.report_kind is None
        return CodexResult(
            run_id=self.run_id,
            executor=record.executor_name,
            executor_version=record.executor_version or version,
            status=record.status.value,
            reason=record.status_reason,
            exit_code=exit_code_for(record.status.value, record.status_reason, failed),
            report_kind=closure.report_kind,
            report_path=closure.report_path,
            diagnostic=closure.diagnostic,
            codex_exit_code=codex_exit_code,
        )


def summary_lines(result: CodexResult) -> list[str]:
    reason = f" ({result.reason})" if result.reason else ""
    lines = [
        f"Executor: {result.executor} {result.executor_version}",
        f"Run {result.run_id}: {result.status}{reason}",
    ]
    if result.report_path is not None:
        lines.append(f"Report: {result.report_path}")
    if result.diagnostic:
        lines.append(f"! {result.diagnostic}")
    lines.append(UNKNOWN_USAGE)
    return lines
