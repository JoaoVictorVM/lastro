import json
from pathlib import Path

import pytest

from lastro.codex.launcher import result_json
from lastro.codex.lifecycle import UNKNOWN_USAGE, exit_code_for, summary_lines
from lastro.codex.preflight import CodexPreflightError
from lastro.history import HistoryUnavailableError

from .codex_support import CodexProject, make_project


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CodexProject:
    monkeypatch.chdir(tmp_path)
    return make_project(tmp_path)


def test_executor_and_version_in_report(project: CodexProject) -> None:
    run_id = project.create_run()
    result = project.launch(run_id)

    record = project.history().get_run(run_id)
    assert (record.executor_name, record.executor_version) == ("codex", "0.166.0")
    assert result.report_path is not None
    report = result.report_path.read_text(encoding="utf-8")
    assert "codex 0.166.0" in report


def test_headless_result_labels_executor(project: CodexProject) -> None:
    run_id = project.create_run()
    result = project.launch(run_id)

    document = json.loads(result_json(result))
    assert document["run_id"] == run_id
    assert document["executor"] == "codex"
    assert document["executor_version"] == "0.166.0"
    assert document["status"] == "interrupted"
    assert document["reason"] == "executor_exited"
    assert document["exit_code"] == 1
    assert document["report_kind"] == "partial"
    lines = summary_lines(result)
    assert lines[0] == "Executor: codex 0.166.0"
    assert UNKNOWN_USAGE in lines


def test_history_failure_prevents_launch(
    project: CodexProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = project.create_run()
    history = project.history()

    def unavailable(run: str) -> object:
        raise HistoryUnavailableError("locked")

    monkeypatch.setattr(history, "claim_launch", unavailable)
    from lastro.codex.launcher import launch
    from lastro.codex.models import LaunchMode

    with pytest.raises(HistoryUnavailableError):
        launch(
            project.context(),
            history,
            run_id,
            LaunchMode.HEADLESS,
            write=project.outputs.append,
            probe=lambda: project.install,
        )
    assert not (project.bin_dir / "exec-call.json").exists()


def test_exit_codes_follow_the_durable_state() -> None:
    assert exit_code_for("completed", "mission_completed") == 0
    assert exit_code_for("completed", "mission_completed", report_failed=True) == 1
    assert exit_code_for("interrupted", "executor_limit") == 3
    assert exit_code_for("interrupted", "executor_exited") == 1
    assert exit_code_for("cancelled", "user_cancelled") == 130


def test_credentials_only_in_ambient_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path, write_env=False)
    monkeypatch.setenv("LASTRO_LOCAL_USERNAME", "u")
    monkeypatch.setenv("LASTRO_LOCAL_PASSWORD", "p")
    with pytest.raises(CodexPreflightError, match=r"define there: LASTRO_LOCAL_USERNAME"):
        project.prepared()
