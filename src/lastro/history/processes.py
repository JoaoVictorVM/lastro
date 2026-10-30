"""Verificação de processo vivo pelo PID em Windows e Linux."""

import os
import sys
from collections.abc import Callable

type ProcessChecker = Callable[[int], bool]

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _STILL_ACTIVE = 259
    _ERROR_ACCESS_DENIED = 5
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    def _native_process_exists(pid: int) -> bool:
        # No Windows, os.kill encerra o processo alvo em vez de apenas testá-lo.
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # Processo de outro usuário existe, mas não pode ser consultado.
            return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
        try:
            exit_code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == _STILL_ACTIVE
        finally:
            _kernel32.CloseHandle(handle)

else:

    def _native_process_exists(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True


def process_exists(pid: int) -> bool:
    """Indica se o PID pertence a um processo em execução."""
    if pid <= 0:
        return False
    return _native_process_exists(pid)
