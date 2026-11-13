import subprocess
import sys
from collections.abc import Callable
from typing import Any

import pytest

from lastro.config.loader import ProjectContext
from lastro.history import RunStatus, TextMission, open_history
from lastro.observability import settings_source
from lastro.observability.models import ExportOutcome, SpanDescriptor
from lastro.observability.worker import ExportWorker


class FakeSender:
    def __init__(self, outcomes: list[ExportOutcome]) -> None:
        self.outcomes = outcomes
        self.batches: list[list[SpanDescriptor]] = []

    def send(self, descriptors: list[SpanDescriptor]) -> ExportOutcome:
        self.batches.append(list(descriptors))
        return self.outcomes.pop(0) if self.outcomes else ExportOutcome("ok")


def worker_for(
    project: ProjectContext, history: Any, run_id: str, sender: FakeSender, **kwargs: Any
) -> ExportWorker:
    return ExportWorker(
        history,
        run_id,
        settings_source(project),
        sender_factory=lambda *_: sender,
        extra_available=lambda: True,
        **kwargs,
    )


@pytest.fixture
def enabled_run(obs_project: Callable[..., ProjectContext]) -> tuple[ProjectContext, Any, str]:
    project = obs_project()
    history = open_history(project)
    run = history.create_run(project, TextMission("Explore employees"))
    history.append_event(run.record.id, "tool_call", tool_name="observe", correlation_id="a1")
    history.append_event(
        run.record.id,
        "policy_decision",
        tool_name="observe",
        correlation_id="a1",
        policy_decision="allow",
        payload={"reason": "read"},
    )
    return project, history, run.record.id


def states(history: Any, run_id: str) -> list[str]:
    return [state for _, state, _ in history.observability_spans(run_id)]


def test_timeout_retains_pending_batch(enabled_run: tuple[ProjectContext, Any, str]) -> None:
    project, history, run_id = enabled_run
    sender = FakeSender([ExportOutcome("retry", "export_timeout")])

    result = worker_for(project, history, run_id, sender).cycle()

    assert result.last_code == "export_timeout"
    assert states(history, run_id) == ["waiting_end", "pending", "pending"]
    status = history.observability_status(run_id)
    assert status.export_status == "pending"
    assert status.last_error_code == "export_timeout"
    again = worker_for(project, history, run_id, FakeSender([])).cycle()
    assert again.sent == 0  # backoff: nada elegível antes do próximo atraso


def test_ack_requires_current_lease(enabled_run: tuple[ProjectContext, Any, str]) -> None:
    project, history, run_id = enabled_run
    first = worker_for(project, history, run_id, FakeSender([]))
    first.project()
    assert history.claim_observability_lease(run_id, first.token)
    batch = history.take_observability_batch(run_id, first.token)
    ids = [item.event_id for item in batch]

    assert not history.claim_observability_lease(run_id, "other-token")
    assert history.acknowledge_observability(run_id, "other-token", ids) == 0
    assert history.acknowledge_observability(run_id, first.token, ids) == len(ids)
    assert states(history, run_id) == ["waiting_end", "acknowledged", "acknowledged"]


def test_partial_success_is_not_acknowledged_or_retried(
    enabled_run: tuple[ProjectContext, Any, str],
) -> None:
    project, history, run_id = enabled_run
    sender = FakeSender([ExportOutcome("rejected", "export_partial")])

    worker_for(project, history, run_id, sender).cycle()
    history.unblock_observability(run_id)
    retry = worker_for(project, history, run_id, FakeSender([])).drain(force=True)

    assert states(history, run_id) == ["waiting_end", "rejected", "rejected"]
    assert retry.sent == 0
    status = history.observability_status(run_id)
    assert status.rejected_spans == 2
    assert all(
        descriptor is not None for _, _, descriptor in history.observability_spans(run_id)[1:]
    )


def test_auth_error_blocks_without_busy_retry(
    enabled_run: tuple[ProjectContext, Any, str],
) -> None:
    project, history, run_id = enabled_run
    sender = FakeSender([ExportOutcome("blocked", "export_unauthorized")])
    worker = worker_for(project, history, run_id, sender)

    worker.cycle()
    for _ in range(3):
        worker.cycle()

    assert len(sender.batches) == 1
    status = history.observability_status(run_id)
    assert (status.export_status, status.last_error_code) == ("blocked", "export_unauthorized")
    assert states(history, run_id).count("pending") == 2


def test_shutdown_has_bounded_drain(enabled_run: tuple[ProjectContext, Any, str]) -> None:
    project, history, run_id = enabled_run
    ticks = iter(range(100))
    sender = FakeSender([ExportOutcome("ok")] * 10)
    worker = worker_for(
        project, history, run_id, sender, monotonic=lambda: float(next(ticks)), poll_seconds=0.01
    )
    worker.start()
    history.finish_run(run_id, RunStatus.COMPLETED)

    worker.stop(drain_budget=3.0)

    assert set(states(history, run_id)) == {"acknowledged"}
    status = history.observability_status(run_id)
    assert status.export_status == "idle"
    assert status.pending_spans == 0


def test_disabled_path_does_not_import_sdk(
    obs_project: Callable[..., ProjectContext],
) -> None:
    project = obs_project(enabled=False)
    code = (
        "import sys\n"
        "from lastro.config.loader import load_project\n"
        "from lastro.history import TextMission, open_history\n"
        "from lastro.observability import drain_run, start_worker\n"
        "from pathlib import Path\n"
        f"project = load_project(Path(r'{project.config_path}'), 'homolog')\n"
        "history = open_history(project)\n"
        "run = history.create_run(project, TextMission('x'))\n"
        "assert history.observability_context(run.record.id) is None\n"
        "assert start_worker(project, history, run.record.id) is None\n"
        "drain_run(project, history, run.record.id)\n"
        "prefixes = ('opentelemetry.sdk', 'opentelemetry.exporter')\n"
        "loaded = [m for m in sys.modules if m.startswith(prefixes)]\n"
        "print(loaded)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        cwd=project.config_path.parent,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"
