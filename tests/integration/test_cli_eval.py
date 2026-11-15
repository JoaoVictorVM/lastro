import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from lastro.cli import eval_cmd
from lastro.cli.app import app
from lastro.config.loader import ProjectContext
from lastro.evaluation.models import StopReason
from lastro.evaluation.runner import RunnerDeps

from .eval_support import FakeDemo, FakeExecutor, FakeJudge, small_suite


@pytest.fixture
def fake_deps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., dict[str, Any]]:
    def install(**executor_kwargs: Any) -> dict[str, Any]:
        state: dict[str, Any] = {"demo": FakeDemo(tmp_path / "demo.log")}
        state["executor"] = FakeExecutor(state["demo"], **executor_kwargs)

        def factory(
            history: Any, project_dir: Path, project_id: str, suite_id: str, *rest: Any
        ) -> RunnerDeps:
            progress = rest[-1]
            return RunnerDeps(
                history=history,
                project_dir=project_dir,
                project_id=project_id,
                demo=state["demo"],
                executor=state["executor"],
                judge=FakeJudge(),
                credentials=("u", "p"),
                progress=progress,
                decision_sleep=lambda seconds: None,
                owner_gone=lambda owner: True,
            )

        monkeypatch.setattr(eval_cmd, "DEPS_FACTORY", factory)
        return state

    return install


def invoke(runner: CliRunner, *args: str) -> Any:
    return runner.invoke(app, ["--profile", "homolog", "eval", *args])


def test_demo_core_runs_thirty_trials_without_stdin(
    runner: CliRunner,
    history_context: ProjectContext,
    fake_deps: Callable[..., dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = fake_deps()
    monkeypatch.setattr(sys, "stdin", None)
    result = invoke(runner, "run", "demo-core", "--json")
    assert result.exit_code == 0, result.output
    summary = json.loads(result.stdout)
    assert summary["trials"]["planned"] == 30 and summary["trials"]["valid"] == 30
    assert summary["state"] in {"awaiting_review", "completed"}
    assert len(state["executor"].calls) == 30 and len(state["demo"].resets) == 30
    assert Path(summary["public_report"]).is_file()
    report = Path(summary["public_report"]).read_text(encoding="utf-8")
    assert "| `full` | 5 | 5 | 0 |" in report


def test_progress_uses_stderr_in_json_mode(
    runner: CliRunner,
    history_context: ProjectContext,
    fake_deps: Callable[..., dict[str, Any]],
    tmp_path: Path,
) -> None:
    fake_deps()
    result = invoke(runner, "run", str(small_suite(tmp_path)), "--json")
    assert result.exit_code == 0, result.output
    json.loads(result.stdout)
    assert "run 1/4 · condition" in result.stderr and "ETA unavailable" in result.stderr
    assert "run 4/4" in result.stderr and "ETA 0m" in result.stderr
    assert "run 1/4" not in result.stdout


def test_resume_exit_code_and_pending_state(
    runner: CliRunner,
    history_context: ProjectContext,
    fake_deps: Callable[..., dict[str, Any]],
    tmp_path: Path,
) -> None:
    state = fake_deps(statuses={2: StopReason.USAGE_CAP})
    result = invoke(runner, "run", str(small_suite(tmp_path)), "--json")
    assert result.exit_code == 4, result.output
    summary = json.loads(result.stdout)
    assert summary["state"] == "paused" and summary["trials"]["paused"] == 1
    assert "lastro eval resume" in result.stderr
    state["executor"] = FakeExecutor(state["demo"])
    resumed = invoke(runner, "resume", summary["suite_run_id"], "--json")
    assert resumed.exit_code == 0, resumed.output
    assert json.loads(resumed.stdout)["trials"]["valid"] == 4
    shown = invoke(runner, "show", summary["suite_run_id"])
    assert shown.exit_code == 0 and "4 valid" in shown.stdout


def test_review_import_regenerates_results(
    runner: CliRunner,
    history_context: ProjectContext,
    fake_deps: Callable[..., dict[str, Any]],
    tmp_path: Path,
) -> None:
    fake_deps()
    result = invoke(runner, "run", str(small_suite(tmp_path)), "--json")
    suite_id = json.loads(result.stdout)["suite_run_id"]
    exported = invoke(runner, "review", "export", suite_id)
    assert exported.exit_code == 0, exported.output
    path = Path(exported.stdout.split("Review file: ", 1)[1].splitlines()[0].strip())
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    filled = tmp_path / "filled.jsonl"
    filled.write_text(
        "\n".join(
            json.dumps({**line, "decision": "match", "bug_id": "D02", "reason": "ok"})
            for line in lines
        ),
        encoding="utf-8",
    )
    imported = invoke(
        runner, "review", "import", suite_id, "--file", str(filled), "--reviewer", "ana"
    )
    assert imported.exit_code == 0, imported.output
    assert "results revision 2" in imported.stdout and "agreement 100.0%" in imported.stdout
    final = json.loads(invoke(runner, "show", suite_id, "--json").stdout)
    assert final["state"] == "completed" and final["results_revision"] == 2
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({**lines[0], "decision": "match", "reason": "x"}), encoding="utf-8")
    rejected = invoke(runner, "review", "import", suite_id, "--file", str(bad), "--reviewer", "ana")
    assert rejected.exit_code == 2 and "review_manifest_mismatch" in rejected.stderr


def test_missing_eval_extra_has_install_hint(
    runner: CliRunner,
    history_context: ProjectContext,
    fake_deps: Callable[..., dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_deps()
    monkeypatch.setitem(sys.modules, "yaml", None)
    result = invoke(runner, "run", "demo-core")
    assert result.exit_code == 2
    assert "eval_extra_missing" in result.stderr and "lastro[eval]" in result.stderr


def test_invalid_suite_and_executor_are_usage_errors(
    runner: CliRunner, history_context: ProjectContext, fake_deps: Callable[..., dict[str, Any]]
) -> None:
    fake_deps()
    assert invoke(runner, "run", "no-such-suite").exit_code == 2
    result = invoke(runner, "run", "demo-core", "--executor", "bash")
    assert result.exit_code == 2 and "unknown executor" in result.stderr
    assert invoke(runner, "show", "eval_missing").exit_code == 2
