import asyncio
import re
from collections.abc import Callable
from typing import Any

import pytest
from typer.testing import CliRunner

from lastro.browser.events import record_call
from lastro.browser.policy import Decision
from lastro.browser.tools import BrowserTools
from lastro.cli import tail_cmd
from lastro.cli.app import app
from lastro.config.limits import RunLimits
from lastro.config.loader import ProjectContext
from lastro.execution.models import WriteIntentInput
from lastro.execution.supervisor import stop_run
from lastro.history import (
    CreatedRecordInput,
    ReportConflictError,
    RunStatus,
    TextMission,
    open_history,
)
from lastro.history.store import HistoryStore
from lastro.reporting.builder import ReportBuilder

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _start(context: ProjectContext) -> tuple[HistoryStore, str]:
    history = open_history(context)
    return history, history.create_run(context, TextMission("Explore employees")).record.id


def _unknown_write(history: HistoryStore, run_id: str) -> str:
    history.reserve_action(run_id, "w1", "submit")
    intent = history.prepare_write(
        WriteIntentInput(
            run_id=run_id,
            action_id="w1",
            binding_id="employee-update",
            operation="update",
            entity="employee",
            record_id="3",
            tenant="t1",
            detail_path="/employees/3/edit",
            before={"email": "a@example.com"},
            intended={"email": "b@example.com"},
            comparable=True,
        )
    )
    history.mark_write_sending(intent.id)
    return intent.id


def test_partial_report_has_reason_pending_and_created_records(
    history_context: ProjectContext,
) -> None:
    history, run_id = _start(history_context)
    intent_id = _unknown_write(history, run_id)
    record_call(history, run_id, "w1", "submit", {}, Decision("allow", "default", ""))
    history.record_created(
        CreatedRecordInput(
            run_id=run_id,
            action_id="w1",
            entity="employee",
            system_identifier="99",
            tenant="t1",
            expected_tenant="t1",
            cleanup_required=True,
            detail_path="/employees/99",
            cleanup_instructions="Remove manually.",
        )
    )
    outcome = stop_run(history, run_id)
    assert outcome.report_path is not None
    text = outcome.report_path.read_text(encoding="utf-8")
    assert "## Execution limits and pending work" in text
    assert "Stopped: cancelled (user_cancelled)" in text
    assert intent_id in text and "response_unknown" in text
    assert "created_record" in text and "cleanup_pending" in text
    kinds = {item.kind for item in outcome.summary.pending_items} if outcome.summary else set()
    assert {"write", "created_record"} <= kinds


def test_warning_reaches_tail_and_mcp(
    make_auth_project: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    context = make_auth_project().context()
    context.profile.run_limits = RunLimits(max_actions=5, max_no_progress_actions=50)
    history = open_history(context)
    run_id = history.create_run(context, TextMission("Explore")).record.id
    runtime = BrowserTools(context, history, run_id)

    async def scenario() -> list[dict[str, Any]]:
        return [
            await runtime.call("navigate", {"url": f"https://evil.example/{index}"})
            for index in range(4)
        ]

    results = asyncio.run(scenario())
    assert "execution_notice" not in results[2]
    assert results[3]["execution_notice"]["message"] == "80% of action budget used; wrap up."
    history.finish_run(run_id, RunStatus.COMPLETED)
    monkeypatch.chdir(context.loaded.project_dir)
    result = CliRunner().invoke(app, ["--profile", "demo", "tail", run_id])
    assert result.exit_code == 0, result.output
    lines = [line for line in ANSI.sub("", result.output).splitlines() if "WARNING" in line]
    assert len(lines) == 1 and "(4/5 actions)" in lines[0]
    assert tail_cmd.POLL_INTERVAL_SECONDS > 0


def test_summary_contract_for_future_autonomous_consumer(
    history_context: ProjectContext,
) -> None:
    history, run_id = _start(history_context)
    _unknown_write(history, run_id)
    stop_run(history, run_id)
    data = history.execution_summary(run_id).as_json()
    assert set(data) >= {
        "run_id",
        "status",
        "reason",
        "actions_used",
        "max_actions",
        "pending_items",
        "report_kind",
        "report_path",
    }
    assert (data["status"], data["reason"], data["report_kind"]) == (
        "cancelled",
        "user_cancelled",
        "partial",
    )
    assert data["report_path"] == "report.partial.md"
    write = next(item for item in data["pending_items"] if item["kind"] == "write")
    assert write["state"] == "response_unknown" and write["requires_manual_review"] is True


def test_late_response_does_not_overwrite_report(history_context: ProjectContext) -> None:
    history, run_id = _start(history_context)
    intent_id = _unknown_write(history, run_id)
    outcome = stop_run(history, run_id)
    assert outcome.report_path is not None
    before = outcome.report_path.read_bytes()
    # Resposta tardia fecha a ambiguidade no histórico, mas não reabre a run nem o relatório.
    history.record_write_response(intent_id, 200)
    assert history.get_run(run_id).status == RunStatus.CANCELLED
    with pytest.raises(ReportConflictError):
        ReportBuilder(history).build_partial(run_id, "user_cancelled")
    assert outcome.report_path.read_bytes() == before
