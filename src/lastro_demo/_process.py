"""Operações de processo por pid que funcionam entre invocações diferentes da CLI."""

import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import IO

POLL_SECONDS = 0.1

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _SYNCHRONIZE = 0x00100000
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259
    _WAIT_OBJECT_0 = 0
    _ERROR_ACCESS_DENIED = 5
    _CREATE_BREAKAWAY_FROM_JOB = 0x01000000

    def pid_alive(pid: int) -> bool:
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
        try:
            code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == _STILL_ACTIVE
        finally:
            _kernel32.CloseHandle(handle)

    def wait_exit(pid: int, timeout: float) -> bool:
        handle = _kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
        if not handle:
            return not pid_alive(pid)
        try:
            return _kernel32.WaitForSingleObject(handle, int(timeout * 1000)) == _WAIT_OBJECT_0
        finally:
            _kernel32.CloseHandle(handle)

    def force_kill(pid: int) -> None:
        # /T encerra também os filhos: o python.exe de um venv é um lançador cujo filho é o
        # interpretador real, que sobreviveria se só o lançador fosse encerrado.
        taskkill = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "taskkill.exe"
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                [str(taskkill), "/T", "/F", "/PID", str(pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=15,
            )
        if not pid_alive(pid):
            return
        handle = _kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
        if not handle:
            return
        try:
            _kernel32.TerminateProcess(handle, 1)
        finally:
            _kernel32.CloseHandle(handle)

    def spawn_detached(
        argv: Sequence[str], *, cwd: Path, log: IO[bytes], env: dict[str, str]
    ) -> subprocess.Popen[bytes]:
        base = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        # O lançador do `uv tool` prende os filhos num job que é encerrado quando a CLI sai;
        # sair do job mantém o servidor vivo. Se o job proibir a saída, sobe dentro dele.
        try:
            return subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                close_fds=True,
                creationflags=base | _CREATE_BREAKAWAY_FROM_JOB,
            )
        except PermissionError:
            return subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                close_fds=True,
                creationflags=base,
            )

else:

    def _reap(pid: int) -> None:
        # Quando o servidor é filho deste processo, um filho encerrado vira zumbi até ser
        # coletado e continuaria parecendo vivo para os.kill(pid, 0).
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)

    def pid_alive(pid: int) -> bool:
        _reap(pid)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def wait_exit(pid: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not pid_alive(pid):
                return True
            time.sleep(POLL_SECONDS)
        return not pid_alive(pid)

    def force_kill(pid: int) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)

    def spawn_detached(
        argv: Sequence[str], *, cwd: Path, log: IO[bytes], env: dict[str, str]
    ) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
        )
