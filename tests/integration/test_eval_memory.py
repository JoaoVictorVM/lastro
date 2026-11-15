import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from lastro.config.loader import ProjectContext
from lastro.evaluation.executors import TrialLaunch
from lastro.evaluation.metrics import measure_attempt
from lastro.evaluation.runner import create_suite, system_identity
from lastro.evaluation.suites import load_suite
from lastro.evaluation.workspace import memory_sha256
from lastro.history import open_history
from lastro.knowledge.retrieval import staleness

from .eval_support import FakeDemo, FakeExecutor, make_runner, small_suite

PAIRED = """    memory: {mode: paired}
pairing: warmup
"""


def knowledge_db(path: Path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS knowledge (scope_key TEXT, fact_key TEXT, "
            "value_sha256 TEXT, validation_state TEXT)"
        )
        conn.execute("INSERT INTO knowledge VALUES (?, 'fact', ?, 'verified')", (marker, marker))


def read_markers(path: Path) -> list[str]:
    if not path.is_file():
        return []
    with sqlite3.connect(path) as conn:
        try:
            return [row[0] for row in conn.execute("SELECT scope_key FROM knowledge")]
        except sqlite3.Error:
            return []


def paired_run(history_context: ProjectContext, tmp_path: Path, runs: int = 5) -> Any:
    history = open_history(history_context)
    loaded = load_suite(
        str(small_suite(tmp_path, conditions="[baseline, full, -memory]", runs=runs, extra=PAIRED))
    )
    suite = create_suite(history.evaluations, loaded, "claude-code", "2.1.0")
    demo = FakeDemo(tmp_path / "demo.log")
    seen: dict[str, list[str]] = {}

    def agent(launch: TrialLaunch, cancel: threading.Event) -> None:
        database = launch.paths.data / "eval-test" / "history.sqlite"
        seen[launch.task.task_id] = read_markers(database)
        knowledge_db(database, launch.task.task_id)

    executor = FakeExecutor(demo, on_launch=agent)
    outcome = make_runner(history, history.project_dir, demo, executor).run(suite.id)
    return history, suite, outcome, seen


def test_five_pairs_share_only_intended_memory(
    history_context: ProjectContext, tmp_path: Path
) -> None:
    history, suite, outcome, seen = paired_run(history_context, tmp_path)
    assert outcome.exit_code == 0
    trials = history.evaluations.list_trials(suite.id)
    warmups = {item.id: item for item in trials if item.role == "warmup"}
    full = [item for item in trials if item.condition == "full" and item.role == "measured"]
    assert len(warmups) == 5 and len(full) == 5
    for trial in full:
        assert trial.parent_trial_id in warmups
        assert seen[trial.id] == [trial.parent_trial_id]
    reduction = [
        item
        for item in history.evaluations.list_results(suite.id)
        if item.metric == "memory_actions_reduction"
    ]
    assert len(reduction) == 1 and reduction[0].planned_n == 5 and reduction[0].n == 5
    measured = [
        item for item in history.evaluations.list_results(suite.id) if item.metric == "actions"
    ]
    assert all(item.stratum["role"] == "measured" for item in measured)


def test_new_condition_cannot_see_previous_findings(
    history_context: ProjectContext, tmp_path: Path
) -> None:
    history, suite, _, seen = paired_run(history_context, tmp_path, runs=2)
    for trial in history.evaluations.list_trials(suite.id):
        if trial.condition in {"-memory", "baseline"}:
            assert seen[trial.id] == []
        if trial.role == "warmup":
            assert seen[trial.id] == []


def test_stale_fact_requires_new_evidence() -> None:
    now = datetime(2026, 11, 14, tzinfo=UTC)
    current = "lastro-demo 0.1.0+base"
    fresh = staleness(now - timedelta(days=30), now, current, current)
    stale = staleness(now - timedelta(days=31), now, current, current)
    assert not fresh.stale and stale.stale


def test_variant_change_invalidates_memory_identity() -> None:
    version = {"version": "0.1.0"}
    base, shifted = system_identity(version, "base"), system_identity(version, "shifted")
    assert base != shifted
    now = datetime(2026, 11, 14, tzinfo=UTC)
    result = staleness(now, now, base, shifted)
    assert result.stale and "version" in " ".join(result.reasons)


def test_memory_identity_hash_ignores_run_ids(tmp_path: Path) -> None:
    first, second = tmp_path / "a.sqlite", tmp_path / "b.sqlite"
    knowledge_db(first, "scope")
    knowledge_db(second, "scope")
    assert memory_sha256(first) == memory_sha256(second)
    knowledge_db(second, "other")
    assert memory_sha256(first) != memory_sha256(second)


def test_untraceable_memory_use_is_unavailable() -> None:
    snapshot = {"findings": [], "criteria_ids": [], "memory": {"stale_misuse": None}}
    measures = {item.metric: item for item in measure_attempt(snapshot, [], {})}
    misuse = measures["stale_memory_misuse"]
    assert misuse.value is None and misuse.unavailable_reason == "memory_use_untraceable"
    traced = {**snapshot, "memory": {"stale_misuse": 0}}
    assert {item.metric: item for item in measure_attempt(traced, [], {})}[
        "stale_memory_misuse"
    ].value == 0.0
