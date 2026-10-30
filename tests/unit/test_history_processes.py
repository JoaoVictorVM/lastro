import os
import subprocess
import sys

from lastro.history import process_exists


def test_current_process_exists() -> None:
    assert process_exists(os.getpid())


def test_finished_process_is_reported_lost() -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()

    assert not process_exists(child.pid)


def test_running_child_exists_until_it_ends() -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert process_exists(child.pid)
    finally:
        child.kill()
        child.wait()
    assert not process_exists(child.pid)


def test_invalid_pids_do_not_exist() -> None:
    assert not process_exists(0)
    assert not process_exists(-1)
