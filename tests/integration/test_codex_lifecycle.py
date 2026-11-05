import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

import pytest

from lastro.codex.launcher import LaunchRefused, RolloutWatcher
from lastro.codex.models import LaunchMode
from lastro.history import LaunchClaimError, RunStatus
from lastro.history.processes import process_exists

from .codex_support import CodexProject, make_project

SUPERVISE = """
import json
import sys
from pathlib import Path

sys.path.insert(0, r"{support}")
import codex_support
from lastro.codex.launcher import launch
from lastro.codex.models import LaunchMode

project = codex_support.CodexProject(Path(r"{project}"), Path(r"{bin_dir}"), [])
result = launch(
    project.context(),
    project.history(),
    "{run_id}",
    LaunchMode.HEADLESS,
    write=print,
    probe=lambda: project.install,
)
print("RESULT", json.dumps(result.document()))
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CodexProject:
    monkeypatch.chdir(tmp_path)
    return make_project(tmp_path)


def test_pending_handoff_survives_start_exit(project: CodexProject) -> None:
    run_id = project.create_run()
    history = project.history()
    assert history.get_active_run() is None

    history._process_checker = lambda pid: False  # type: ignore[assignment]
    history.owner_pid = -1

    assert history.reconcile_orphans() == []
    assert history.get_run(run_id).status is RunStatus.RUNNING


def test_atomic_launch_claim(project: CodexProject) -> None:
    run_id = project.create_run()
    history = project.history()
    before = history.get_run(run_id).config_snapshot

    claimed = history.claim_launch(run_id)

    assert claimed.owner_pid == os.getpid()
    block = cast(dict[str, object], claimed.config_snapshot["codex"])
    frozen = cast(dict[str, object], before["codex"])
    assert block["launch_pending"] is False
    assert block["sandbox_manifest_sha256"] == frozen["sandbox_manifest_sha256"]
    assert {k: v for k, v in claimed.config_snapshot.items() if k != "codex"} == {
        k: v for k, v in before.items() if k != "codex"
    }
    assert history.get_active_run(run_id) is not None
    with pytest.raises(LaunchClaimError):
        history.claim_launch(run_id)


def test_claim_failure_rolls_back(project: CodexProject, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = project.create_run()
    history = project.history()

    def fail(path: Path, record: object) -> None:
        raise OSError("disk")

    monkeypatch.setattr("lastro.history.store.publish_active_pointer", fail)
    with pytest.raises(LaunchClaimError, match="could not be published"):
        history.claim_launch(run_id)

    record = history.get_run(run_id)
    assert record.status is RunStatus.INTERRUPTED
    assert record.status_reason == "launch_publish_failed"
    assert history.get_active_run() is None


def test_second_launch_refused(project: CodexProject) -> None:
    project.behave(events=[{"type": "turn.completed"}])
    run_id = project.create_run()
    project.launch(run_id)
    with pytest.raises(LaunchRefused, match="already ended"):
        project.launch(run_id)


def test_claimed_live_run_refuses_another_session(project: CodexProject) -> None:
    run_id = project.create_run()
    project.history().claim_launch(run_id)
    with pytest.raises(LaunchRefused, match="already launched"):
        project.launch(run_id)


def test_new_run_abandons_unlaunched_handoff(project: CodexProject) -> None:
    first = project.create_run()
    second = project.create_run()

    history = project.history()
    old = history.get_run(first)
    assert old.status is RunStatus.INTERRUPTED
    assert old.status_reason == "launch_abandoned"
    assert history.get_run(second).status is RunStatus.RUNNING


def test_child_crash(project: CodexProject) -> None:
    project.behave(exit=9)
    run_id = project.create_run()

    result = project.launch(run_id)

    assert (result.status, result.reason) == ("interrupted", "executor_exited")
    assert result.exit_code == 1
    assert result.codex_exit_code == 9
    assert result.report_kind == "partial"


def test_spawn_failure_ends_the_claimed_run(project: CodexProject) -> None:
    run_id = project.create_run()

    def spawn(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        raise OSError("no exec")

    result = project.launch(run_id, spawn=spawn)

    assert result.status == "interrupted"
    assert project.history().get_active_run() is None


def test_terminal_run_not_overwritten(project: CodexProject) -> None:
    project.behave(finish_status="completed", exit=0)
    run_id = project.create_run()

    result = project.launch(run_id)

    assert result.status == "completed"
    assert result.reason == "mission_completed"
    assert project.history().get_run(run_id).status is RunStatus.COMPLETED


def test_cancel_stops_process_tree(project: CodexProject, tmp_path: Path) -> None:
    project.behave(hang=True)
    run_id = project.create_run()
    (project.bin_dir / "supervise.py").write_text(
        SUPERVISE.format(
            support=str(Path(__file__).parent),
            project=str(project.directory),
            bin_dir=str(project.bin_dir),
            run_id=run_id,
        ),
        encoding="utf-8",
    )
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    supervisor = subprocess.Popen(
        [sys.executable, str(project.bin_dir / "supervise.py")],
        cwd=project.directory,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=flags,
        env=env,
    )
    marker = project.bin_dir / "hanging"
    deadline = time.monotonic() + 90
    while not marker.exists() and time.monotonic() < deadline:
        assert supervisor.poll() is None, supervisor.communicate()
        time.sleep(0.2)
    assert marker.exists()
    child = int(marker.read_text(encoding="utf-8"))

    interrupt = signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT
    supervisor.send_signal(interrupt)
    out, err = supervisor.communicate(timeout=90)

    assert "RESULT" in out, (out, err)
    document = json.loads(out.split("RESULT ", 1)[1])
    assert (document["status"], document["reason"]) == ("cancelled", "user_cancelled")
    assert document["exit_code"] == 130
    assert not process_exists(child)


def test_interactive_session_uses_no_exec(project: CodexProject) -> None:
    run_id = project.create_run()

    result = project.launch(run_id, LaunchMode.INTERACTIVE)

    argv = project.call("interactive")["argv"]
    assert "--no-daemon" in argv
    assert "exec" not in argv
    assert result.reason == "executor_exited"


def test_rollout_watcher_finds_limit_only_in_this_project(tmp_path: Path) -> None:
    from datetime import date

    project_dir = tmp_path / "p"
    project_dir.mkdir()
    day = date.today()
    folder = tmp_path / "home" / "sessions" / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
    folder.mkdir(parents=True)
    started = time.time() - 5
    other = folder / "rollout-other.jsonl"
    limit = {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "rate_limits": {"rate_limit_reached_type": "usage_limit_reached"},
        },
    }
    meta = {"type": "session_meta", "payload": {"cwd": str(tmp_path / "elsewhere")}}
    other.write_text(json.dumps(meta) + "\n" + json.dumps(limit) + "\n", encoding="utf-8")
    watcher = RolloutWatcher(tmp_path / "home", project_dir, started)
    assert watcher.poll() is None

    mine = folder / "rollout-mine.jsonl"
    meta["payload"]["cwd"] = str(project_dir)
    mine.write_text(json.dumps(meta) + "\n" + "x" * 10 + "\n", encoding="utf-8")
    assert watcher.poll() is None
    with mine.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(limit) + "\n")
    failure = watcher.poll()
    assert failure is not None
    assert failure.reason == "executor_limit"
