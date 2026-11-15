from pathlib import Path

import pytest

from lastro.config.loader import ProjectContext
from lastro.evaluation.models import EvalError, StopReason
from lastro.evaluation.runner import create_suite
from lastro.evaluation.suites import load_suite
from lastro.history import open_history
from lastro.history.evaluation import EvalStoreError

from .eval_support import FakeDemo, FakeExecutor, FakeJudge, make_runner, small_suite


def setup(history_context: ProjectContext, tmp_path: Path, **kwargs: object):  # type: ignore[no-untyped-def]
    history = open_history(history_context)
    loaded = load_suite(str(small_suite(tmp_path, **kwargs)))  # type: ignore[arg-type]
    suite = create_suite(history.evaluations, loaded, "claude-code", "2.1.0")
    demo = FakeDemo(tmp_path / "demo" / "access.log")
    return history, suite, demo


def test_usage_cap_pauses_and_restarts_only_pending_slot(
    history_context: ProjectContext, tmp_path: Path
) -> None:
    history, suite, demo = setup(history_context, tmp_path)
    executor = FakeExecutor(demo, statuses={2: StopReason.USAGE_CAP})
    outcome = make_runner(history, history.project_dir, demo, executor).run(suite.id)
    assert outcome.exit_code == 4 and outcome.suite.status == "paused"
    trials = history.evaluations.list_trials(suite.id)
    assert [item.status for item in trials] == ["valid", "paused", "pending", "pending"]
    paused_trial = trials[1]
    resumed = FakeExecutor(demo)
    final = make_runner(history, history.project_dir, demo, resumed).run(suite.id, resume=True)
    assert final.exit_code == 0
    assert len(resumed.calls) == 3
    assert resumed.calls[0].task.task_id == paused_trial.id
    attempts = history.evaluations.list_attempts(paused_trial.id)
    assert [(item.attempt, item.status) for item in attempts] == [(1, "paused"), (2, "valid")]
    assert len(demo.resets) == 5
    assert {item.status for item in history.evaluations.list_trials(suite.id)} == {"valid"}


def test_judge_resume_does_not_replay_exploration(
    history_context: ProjectContext, tmp_path: Path
) -> None:
    history, suite, demo = setup(history_context, tmp_path)
    executor = FakeExecutor(demo)
    first = make_runner(history, history.project_dir, demo, executor, FakeJudge(limit_at=2))
    outcome = first.run(suite.id)
    assert outcome.exit_code == 4 and outcome.suite.pause_reason == "judge:rate_limit"
    assert len(executor.calls) == 4
    again = FakeExecutor(demo)
    judge = FakeJudge()
    final = make_runner(history, history.project_dir, demo, again, judge).run(suite.id, resume=True)
    assert final.exit_code == 0
    assert again.calls == []
    judgments = history.evaluations.list_judgments(suite.id)
    assert len(judgments) == 4 and {item.status for item in judgments} == {"judged"}
    assert len(judge.prompts) == 3


def test_crash_continues_without_replacing_invalid_sample(
    history_context: ProjectContext, tmp_path: Path
) -> None:
    history, suite, demo = setup(history_context, tmp_path)
    executor = FakeExecutor(demo, statuses={1: "raise"})
    outcome = make_runner(history, history.project_dir, demo, executor).run(suite.id)
    assert outcome.exit_code == 0
    trials = history.evaluations.list_trials(suite.id)
    assert [item.status for item in trials] == ["invalid", "valid", "valid", "valid"]
    (attempt,) = history.evaluations.list_attempts(trials[0].id)
    assert (attempt.status, attempt.reason) == ("invalid", "crash")
    assert len(executor.calls) == 4
    assert history.evaluations.get_suite(suite.id).current_revision >= 1


def test_snapshot_or_version_change_refuses_resume(
    history_context: ProjectContext, tmp_path: Path
) -> None:
    history, suite, demo = setup(history_context, tmp_path)
    executor = FakeExecutor(demo, statuses={1: StopReason.RATE_LIMIT})
    make_runner(history, history.project_dir, demo, executor).run(suite.id)
    newer = FakeExecutor(demo, version="9.9.9")
    with pytest.raises(EvalError) as error:
        make_runner(history, history.project_dir, demo, newer).run(suite.id, resume=True)
    assert error.value.code == "resume_identity_changed"
    assert newer.calls == []


def test_two_resumers_cannot_own_suite(history_context: ProjectContext, tmp_path: Path) -> None:
    history, suite, demo = setup(history_context, tmp_path)
    repo = history.evaluations
    lease = repo.acquire_lease(suite.id, {"host": "other"}, owner_gone=lambda owner: True)
    executor = FakeExecutor(demo)
    with pytest.raises(EvalStoreError) as error:
        make_runner(history, history.project_dir, demo, executor).run(suite.id, resume=True)
    assert error.value.code == "lease_busy"
    assert executor.calls == [] and demo.resets == []
    repo.release_lease(lease)


def test_reused_pid_is_not_killed(
    history_context: ProjectContext,
    tmp_path: Path,
    clock_now: list,
    fake_clock,  # type: ignore[no-untyped-def]
) -> None:
    from datetime import timedelta

    from lastro.history.database import Database
    from lastro.history.evaluation import EvalRepository

    history, suite, _ = setup(history_context, tmp_path)
    repo = EvalRepository(Database(history.database_path), fake_clock)
    repo.acquire_lease(suite.id, {"host": "h", "pid": 4242}, owner_gone=lambda owner: True)
    clock_now[0] += timedelta(seconds=31)
    seen: list[dict[str, object]] = []

    def alive(owner: dict[str, object]) -> bool:
        seen.append(owner)
        return False

    with pytest.raises(EvalStoreError) as error:
        repo.acquire_lease(suite.id, {"host": "h", "pid": 1}, owner_gone=alive)
    assert error.value.code == "owner_unverified"
    assert seen == [{"host": "h", "pid": 4242}]
    taken = repo.acquire_lease(suite.id, {"host": "h", "pid": 1}, owner_gone=lambda owner: True)
    assert taken.generation == 2


def test_lost_lease_stops_new_effects(history_context: ProjectContext, tmp_path: Path) -> None:
    history, suite, demo = setup(history_context, tmp_path)
    repo = history.evaluations

    def steal(launch, cancel):  # type: ignore[no-untyped-def]
        with repo._db.connection() as conn:  # pyright: ignore[reportPrivateUsage]
            conn.execute("UPDATE eval_suites SET lease_token = 'other' WHERE id = ?", (suite.id,))

    executor = FakeExecutor(demo, on_launch=steal)
    with pytest.raises(EvalStoreError) as error:
        make_runner(history, history.project_dir, demo, executor).run(suite.id)
    assert error.value.code == "lease_lost"
    assert len(executor.calls) == 1 and len(demo.resets) == 1


def test_sigint_pauses_current_slot(history_context: ProjectContext, tmp_path: Path) -> None:
    history, suite, demo = setup(history_context, tmp_path)

    def interrupt(launch, cancel):  # type: ignore[no-untyped-def]
        raise KeyboardInterrupt

    executor = FakeExecutor(demo, on_launch=interrupt)
    outcome = make_runner(history, history.project_dir, demo, executor).run(suite.id)
    assert outcome.exit_code == 130 and outcome.suite.status == "paused"
    first = history.evaluations.list_trials(suite.id)[0]
    assert first.status == "paused"
    (attempt,) = history.evaluations.list_attempts(first.id)
    assert (attempt.status, attempt.reason) == ("paused", "interrupted")
    final = make_runner(history, history.project_dir, demo, FakeExecutor(demo)).run(
        suite.id, resume=True
    )
    assert final.exit_code == 0
