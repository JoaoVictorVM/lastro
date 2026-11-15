import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lastro.config.loader import ProjectContext
from lastro.evaluation.demo import ManagedDemo, check_version, read_window
from lastro.evaluation.metrics import access_summary
from lastro.evaluation.models import DemoSpec, EvalError
from lastro.evaluation.runner import create_suite
from lastro.evaluation.suites import load_suite
from lastro.history import open_history
from lastro_demo import __version__ as DEMO_VERSION
from lastro_demo.app import create_app
from lastro_demo.config import DemoConfig, load_seed_users
from lastro_demo.flags import FlagState

from .eval_support import FakeDemo, FakeExecutor, make_runner, small_suite


def run_suite(
    history_context: ProjectContext, tmp_path: Path, demo: FakeDemo, **kwargs: Any
) -> Any:
    history = open_history(history_context)
    loaded = load_suite(str(small_suite(tmp_path)))
    suite = create_suite(history.evaluations, loaded, "claude-code", "2.1.0")
    executor = kwargs.pop("executor", None) or FakeExecutor(demo)
    outcome = make_runner(history, history.project_dir, demo, executor).run(suite.id)
    return history, suite, outcome, executor


def test_every_trial_resets_and_captures_version(
    history_context: ProjectContext, tmp_path: Path
) -> None:
    demo = FakeDemo(tmp_path / "demo.log")
    history, suite, outcome, executor = run_suite(history_context, tmp_path, demo)
    assert outcome.exit_code == 0
    trials = history.evaluations.list_trials(suite.id)
    assert len(demo.resets) == len(trials) == len(executor.calls) == 4
    for trial in trials:
        (attempt,) = history.evaluations.list_attempts(trial.id)
        assert attempt.reset is not None and attempt.reset["status"] == "ok"
        assert attempt.version is not None and attempt.version["version"] == DEMO_VERSION
        assert attempt.access_start is not None and attempt.access_end is not None


def test_failed_reset_is_invalid_without_login(
    history_context: ProjectContext, tmp_path: Path
) -> None:
    demo = FakeDemo(tmp_path / "demo.log", reset_ok=lambda index: index != 2)
    history, suite, _, executor = run_suite(history_context, tmp_path, demo)
    trials = history.evaluations.list_trials(suite.id)
    assert [item.status for item in trials] == ["valid", "invalid", "valid", "valid"]
    (attempt,) = history.evaluations.list_attempts(trials[1].id)
    assert attempt.reason == "reset" and attempt.run_id is None
    assert len(executor.calls) == 3
    results = history.evaluations.list_results(suite.id)
    cell = next(
        item
        for item in results
        if item.metric == "actions" and item.stratum["condition"] == trials[1].condition
    )
    assert (cell.n, cell.invalid_n, cell.planned_n) == (1, 1, 2)


def test_version_mismatch_is_invalid(history_context: ProjectContext, tmp_path: Path) -> None:
    demo = FakeDemo(
        tmp_path / "demo.log", version_payload=lambda spec: {"version": "0.0.0", "defects": []}
    )
    history, suite, _, executor = run_suite(history_context, tmp_path, demo)
    attempts = [
        history.evaluations.list_attempts(item.id)[0]
        for item in history.evaluations.list_trials(suite.id)
    ]
    assert {item.reason for item in attempts} == {"version"}
    assert executor.calls == []
    with pytest.raises(EvalError):
        check_version(
            {"version": DEMO_VERSION, "defects": ["d01"], "scenarios": [], "variant": "base"},
            DemoSpec(defects=("d02",), scenarios=()),
            DEMO_VERSION,
        )


def test_reproduction_does_not_reset_parent_records(
    history_context: ProjectContext, tmp_path: Path
) -> None:
    demo = FakeDemo(tmp_path / "demo.log")
    resets_during: list[int] = []

    def child_steps(launch: Any, cancel: Any) -> None:
        before = len(demo.resets)
        demo.request("POST", "/employees/1", "update")
        # A reprodução acontece dentro da mesma tentativa, sem chamada de reset no meio.
        demo.request("GET", "/employees/1", "read")
        resets_during.append(len(demo.resets) - before)

    executor = FakeExecutor(demo, on_launch=child_steps)
    run_suite(history_context, tmp_path, demo, executor=executor)
    assert resets_during == [0, 0, 0, 0]


def test_owned_demo_never_resets_user_instance(tmp_path: Path) -> None:
    state = tmp_path / "demo"
    first = ManagedDemo(state, owner_token="suite-a", package_version=DEMO_VERSION)
    first.claim()
    other = ManagedDemo(state, owner_token="suite-b", package_version=DEMO_VERSION)
    with pytest.raises(EvalError) as error:
        other.ensure(1)
    assert error.value.code == "lease_busy"
    first.release()
    other.claim()
    user_state = tmp_path / "user-demo"
    assert not user_state.exists()


def test_access_log_not_self_report_counts_violations(
    history_context: ProjectContext, tmp_path: Path
) -> None:
    demo = FakeDemo(tmp_path / "demo.log")
    executor = FakeExecutor(
        demo,
        requests=[
            ("POST", "/employees/3/deactivate", "deactivate"),
            ("POST", "/employees/1", "update"),
        ],
    )
    executor.findings = []
    history, suite, _, _ = run_suite(history_context, tmp_path, demo, executor=executor)
    results = history.evaluations.list_results(suite.id)
    violations = [item for item in results if item.metric == "policy_violations"]
    assert {item.mean for item in violations} == {2.0}
    for trial in history.evaluations.list_trials(suite.id):
        attempt = history.evaluations.get_attempt(trial.selected_attempt_id or "")
        window = read_window(demo.log, attempt.access_start or 0, attempt.access_end or 0)
        assert all(entry["action"] != "control" for entry in window.entries)


def test_missing_log_prevents_zero_claim(history_context: ProjectContext, tmp_path: Path) -> None:
    demo = FakeDemo(tmp_path / "demo.log")

    def corrupt(launch: Any, cancel: Any) -> None:
        with demo.log.open("a", encoding="utf-8") as handle:
            handle.write("{not json\n")

    executor = FakeExecutor(demo, on_launch=corrupt)
    history, suite, _, _ = run_suite(history_context, tmp_path, demo, executor=executor)
    violations = [
        item
        for item in history.evaluations.list_results(suite.id)
        if item.metric == "policy_violations"
    ]
    assert violations and all(item.mean is None and item.n == 0 for item in violations)
    assert all("unavailable:access_log_unavailable" in item.limitations for item in violations)


def test_log_context_comes_from_server(tmp_path: Path) -> None:
    config = DemoConfig(state_dir=tmp_path / "state")
    client = TestClient(create_app(config, FlagState.only("d02")))
    user = next(item for item in load_seed_users({}) if item.key == "aurora")
    client.post("/login", data={"username": user.email, "password": user.password})
    client.get("/employees", headers={"X-Demo-Request-Id": "forged"})
    lines = [json.loads(line) for line in config.access_log_path.read_text("utf-8").splitlines()]
    listing = next(item for item in lines if item["path"] == "/employees")
    assert listing["request_id"] != "forged" and len(listing["request_id"]) == 32
    assert listing["tenant"] == "aurora" and listing["action"] == "read"
    window = read_window(config.access_log_path, 0, config.access_log_path.stat().st_size)
    summary = access_summary(window, expected_tenant="aurora")
    assert summary["intact"] and summary["writes"] == 0
