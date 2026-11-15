from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from lastro.config.loader import ProjectContext
from lastro.evaluation.models import HeadlessResult, StopReason, Usage
from lastro.evaluation.runner import create_suite
from lastro.evaluation.suites import load_suite
from lastro.history import open_history

from .eval_support import FakeDemo, FakeExecutor, FakeJudge, make_runner, small_suite

PAIRED = """    memory: {mode: paired}
pairing: warmup
"""


@dataclass
class CostlyExecutor(FakeExecutor):
    cost: int | None = 1_000_000_000
    child_cost: int | None = 250_000_000

    def launch(self, launch: Any, cancel: Any) -> HeadlessResult:
        result = super().launch(launch, cancel)
        return HeadlessResult(
            status=result.status,
            executor=result.executor,
            run_ref="run_x",
            usage=Usage(cost_nusd=self.cost, cost_source="test"),
            reproduction_usage=Usage(cost_nusd=self.child_cost) if self.child_cost else None,
        )


@dataclass
class CostlyJudge(FakeJudge):
    def ask(self, prompt: str, workdir: Path) -> tuple[str, Usage, str | None]:
        text, _, model = super().ask(prompt, workdir)
        return text, Usage(cost_nusd=10_000_000), model


def suite_with(history_context: ProjectContext, tmp_path: Path, **kwargs: Any) -> Any:
    history = open_history(history_context)
    loaded = load_suite(str(small_suite(tmp_path, **kwargs)))
    return history, create_suite(history.evaluations, loaded, "claude-code", "2.1.0")


def test_suite_budget_includes_warmup_reproduction_and_judge(
    history_context: ProjectContext, tmp_path: Path
) -> None:
    history, suite = suite_with(
        history_context, tmp_path, conditions="[full]", runs=1, extra=PAIRED
    )
    demo = FakeDemo(tmp_path / "demo.log")
    runner = make_runner(history, history.project_dir, demo, CostlyExecutor(demo), CostlyJudge())
    assert runner.run(suite.id).exit_code == 0
    entries = history.evaluations.list_budget(suite.id)
    components = sorted(entry.component for entry in entries)
    assert components == ["exploration", "judge", "reproduction", "reproduction", "warmup"]
    assert all(entry.status == "settled" for entry in entries)
    total = history.evaluations.committed_nusd(suite.id)
    assert total == 2 * 1_000_000_000 + 2 * 250_000_000 + 10_000_000


def test_cap_pauses_before_paid_call(history_context: ProjectContext, tmp_path: Path) -> None:
    history, suite = suite_with(history_context, tmp_path, conditions="[full]", runs=2)
    repo = history.evaluations
    repo.reserve_budget(suite.id, "exploration", "external", 30_000_000_000, 30_000_000_000)
    repo.update_budget(suite.id, "external", "settled", actual_nusd=30_000_000_000)
    demo = FakeDemo(tmp_path / "demo.log")
    executor = FakeExecutor(demo)
    from lastro.evaluation.runner import NATIVE_RESERVATION_NUSD

    suite_native = suite
    with history.evaluations._db.connection() as conn:  # pyright: ignore[reportPrivateUsage]
        conn.execute("UPDATE eval_suites SET executor_name = 'native' WHERE id = ?", (suite.id,))
    executor.name = "native"
    executor.version = "2.1.0"
    outcome = make_runner(history, history.project_dir, demo, executor).run(suite_native.id)
    assert outcome.exit_code == 4 and outcome.message == "cost_cap"
    assert executor.calls == []
    assert NATIVE_RESERVATION_NUSD > 0


def test_uncertain_charge_survives_resume(history_context: ProjectContext, tmp_path: Path) -> None:
    history, suite = suite_with(history_context, tmp_path, conditions="[full]", runs=1)
    repo = history.evaluations
    lease = repo.acquire_lease(suite.id, {"host": "dead"}, owner_gone=lambda owner: True)
    (trial,) = repo.list_trials(suite.id)
    attempt = repo.start_attempt(
        lease, trial.id, "evals/x/{attempt}", {"name": "claude-code", "version": "2.1.0"}
    )
    repo.reserve_budget(
        suite.id, "exploration", f"exploration:{attempt.id}", 500, 10**12, attempt.id
    )
    repo.update_budget(suite.id, f"exploration:{attempt.id}", "sent")
    with repo._db.connection() as conn:  # pyright: ignore[reportPrivateUsage]
        conn.execute(
            "UPDATE eval_suites SET lease_expires_at = '2000-01-01' WHERE id = ?", (suite.id,)
        )
    demo = FakeDemo(tmp_path / "demo.log")
    make_runner(history, history.project_dir, demo, FakeExecutor(demo)).run(suite.id, resume=True)
    orphan = next(item for item in repo.list_budget(suite.id) if item.attempt_id == attempt.id)
    assert orphan.status == "uncertain"
    assert repo.committed_nusd(suite.id) >= 500
    with pytest.raises(Exception, match="cannot be released"):
        repo.update_budget(suite.id, orphan.request_key, "released")


def test_external_unknown_cost_keeps_limitation(
    history_context: ProjectContext, tmp_path: Path
) -> None:
    history, suite = suite_with(history_context, tmp_path, conditions="[full]", runs=2)
    demo = FakeDemo(tmp_path / "demo.log")
    make_runner(history, history.project_dir, demo, FakeExecutor(demo)).run(suite.id)
    results = history.evaluations.list_results(suite.id)
    cost = next(item for item in results if item.metric == "cost_usd")
    assert cost.n == 0 and cost.mean is None
    assert "unavailable:cost_unavailable" in cost.limitations
    from lastro.evaluation.export import collect

    assert "external_cost_unavailable" in collect(history, suite.id).limitations


def test_no_native_cost_double_count(history_context: ProjectContext, tmp_path: Path) -> None:
    history, suite = suite_with(history_context, tmp_path, conditions="[full]", runs=1)
    with history.evaluations._db.connection() as conn:  # pyright: ignore[reportPrivateUsage]
        conn.execute("UPDATE eval_suites SET executor_name = 'native' WHERE id = ?", (suite.id,))
    demo = FakeDemo(tmp_path / "demo.log")
    executor = CostlyExecutor(demo, name="native", child_cost=None)
    make_runner(history, history.project_dir, demo, executor, FakeJudge()).run(suite.id)
    (exploration,) = [
        item
        for item in history.evaluations.list_budget(suite.id)
        if item.component == "exploration"
    ]
    assert exploration.source_ref == "native-ledger:run_x"
    assert exploration.actual_nusd == 1_000_000_000
    assert history.evaluations.committed_nusd(suite.id) == 1_000_000_000


def test_stop_reason_values_are_closed() -> None:
    assert {item.value for item in StopReason if item.pauses_suite} == {
        "rate_limit",
        "usage_cap",
        "cost_cap",
    }
