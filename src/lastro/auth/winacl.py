"""DACL owner-only no Windows via advapi32, sem dependência externa.

O descritor é protegido (sem herança) e tem uma única ACE de acesso total para o SID do usuário
do processo. Nenhuma ACE explícita é concedida a SYSTEM ou administradores.
"""

import re
import sys
from pathlib import Path

ACE_PATTERN = re.compile(r"\(([^)]*)\)")


class AclError(OSError):
    pass


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _TOKEN_QUERY = 0x0008
    _TOKEN_USER_CLASS = 1
    _SDDL_REVISION_1 = 1
    _DACL_SECURITY_INFORMATION = 0x00000004
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _CREATE_NEW = 1
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = (
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        )

    _OpenProcessToken = _advapi32.OpenProcessToken
    _OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    _OpenProcessToken.restype = wintypes.BOOL

    _GetTokenInformation = _advapi32.GetTokenInformation
    _GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    _GetTokenInformation.restype = wintypes.BOOL

    _ConvertSidToStringSidW = _advapi32.ConvertSidToStringSidW
    _ConvertSidToStringSidW.argtypes = (wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR))
    _ConvertSidToStringSidW.restype = wintypes.BOOL

    _ConvertStringSidToSidW = _advapi32.ConvertStringSidToSidW
    _ConvertStringSidToSidW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPVOID))
    _ConvertStringSidToSidW.restype = wintypes.BOOL

    _EqualSid = _advapi32.EqualSid
    _EqualSid.argtypes = (wintypes.LPVOID, wintypes.LPVOID)
    _EqualSid.restype = wintypes.BOOL

    _ConvertSddlToSd = _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    _ConvertSddlToSd.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.ULONG),
    )
    _ConvertSddlToSd.restype = wintypes.BOOL

    _ConvertSdToSddl = _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW
    _ConvertSdToSddl.argtypes = (
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.ULONG),
    )
    _ConvertSdToSddl.restype = wintypes.BOOL

    _SetFileSecurityW = _advapi32.SetFileSecurityW
    _SetFileSecurityW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID)
    _SetFileSecurityW.restype = wintypes.BOOL

    _GetFileSecurityW = _advapi32.GetFileSecurityW
    _GetFileSecurityW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    _GetFileSecurityW.restype = wintypes.BOOL

    _GetCurrentProcess = _kernel32.GetCurrentProcess
    _GetCurrentProcess.argtypes = ()
    _GetCurrentProcess.restype = wintypes.HANDLE

    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = (wintypes.HANDLE,)
    _CloseHandle.restype = wintypes.BOOL

    _LocalFree = _kernel32.LocalFree
    _LocalFree.argtypes = (wintypes.LPVOID,)
    _LocalFree.restype = wintypes.LPVOID

    _CreateDirectoryW = _kernel32.CreateDirectoryW
    _CreateDirectoryW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(_SecurityAttributes))
    _CreateDirectoryW.restype = wintypes.BOOL

    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_SecurityAttributes),
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    _CreateFileW.restype = wintypes.HANDLE

    def _fail(operation: str) -> AclError:
        code = ctypes.get_last_error()
        return AclError(code, f"{operation} falhou (erro {code})")

    def current_user_sid() -> str:
        token = wintypes.HANDLE()
        if not _OpenProcessToken(_GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
            raise _fail("OpenProcessToken")
        try:
            size = wintypes.DWORD()
            _GetTokenInformation(token, _TOKEN_USER_CLASS, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            if not _GetTokenInformation(token, _TOKEN_USER_CLASS, buffer, size, ctypes.byref(size)):
                raise _fail("GetTokenInformation")
            # TOKEN_USER começa com SID_AND_ATTRIBUTES, cujo primeiro campo é o ponteiro do SID.
            sid_pointer = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID)).contents
            text = wintypes.LPWSTR()
            if not _ConvertSidToStringSidW(sid_pointer, ctypes.byref(text)):
                raise _fail("ConvertSidToStringSidW")
            try:
                return str(text.value)
            finally:
                _LocalFree(ctypes.cast(text, wintypes.LPVOID))
        finally:
            _CloseHandle(token)

    def owner_only_sddl(*, directory: bool) -> str:
        inheritance = "OICI" if directory else ""
        return f"D:P(A;{inheritance};FA;;;{current_user_sid()})"

    class _Descriptor:
        def __init__(self, sddl: str) -> None:
            self.pointer = wintypes.LPVOID()
            if not _ConvertSddlToSd(sddl, _SDDL_REVISION_1, ctypes.byref(self.pointer), None):
                raise _fail("ConvertStringSecurityDescriptorToSecurityDescriptorW")

        def attributes(self) -> _SecurityAttributes:
            return _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), self.pointer, False)

        def release(self) -> None:
            _LocalFree(self.pointer)

    def apply_owner_only(path: Path, *, directory: bool) -> None:
        descriptor = _Descriptor(owner_only_sddl(directory=directory))
        try:
            flags = _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION
            if not _SetFileSecurityW(str(path), flags, descriptor.pointer):
                raise _fail("SetFileSecurityW")
        finally:
            descriptor.release()

    def create_owner_only_directory(path: Path) -> None:
        """Cria o diretório já com a DACL owner-only; se existir, reaplica a DACL."""
        descriptor = _Descriptor(owner_only_sddl(directory=True))
        try:
            attributes = descriptor.attributes()
            created = _CreateDirectoryW(str(path), ctypes.byref(attributes))
            if not created and not path.is_dir():
                raise _fail("CreateDirectoryW")
        finally:
            descriptor.release()
        apply_owner_only(path, directory=True)

    def create_owner_only_file(path: Path) -> None:
        """Cria um arquivo vazio que já nasce com a DACL owner-only, antes de qualquer conteúdo."""
        descriptor = _Descriptor(owner_only_sddl(directory=False))
        try:
            attributes = descriptor.attributes()
            handle = _CreateFileW(
                str(path),
                _GENERIC_WRITE,
                0,
                ctypes.byref(attributes),
                _CREATE_NEW,
                _FILE_ATTRIBUTE_NORMAL,
                None,
            )
            if handle is None or handle == _INVALID_HANDLE_VALUE:
                raise _fail("CreateFileW")
            _CloseHandle(handle)
        finally:
            descriptor.release()

    def read_dacl_sddl(path: Path) -> str:
        size = wintypes.DWORD()
        _GetFileSecurityW(str(path), _DACL_SECURITY_INFORMATION, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not _GetFileSecurityW(
            str(path), _DACL_SECURITY_INFORMATION, buffer, size, ctypes.byref(size)
        ):
            raise _fail("GetFileSecurityW")
        text = wintypes.LPWSTR()
        if not _ConvertSdToSddl(
            buffer, _SDDL_REVISION_1, _DACL_SECURITY_INFORMATION, ctypes.byref(text), None
        ):
            raise _fail("ConvertSecurityDescriptorToStringSecurityDescriptorW")
        try:
            return str(text.value)
        finally:
            _LocalFree(ctypes.cast(text, wintypes.LPVOID))

    def same_sid(first: str, second: str) -> bool:
        """Compara SIDs em texto, aceitando aliases SDDL como `LA` ou `BA`."""
        left = wintypes.LPVOID()
        right = wintypes.LPVOID()
        if not _ConvertStringSidToSidW(first, ctypes.byref(left)):
            return False
        try:
            if not _ConvertStringSidToSidW(second, ctypes.byref(right)):
                return False
            try:
                return bool(_EqualSid(left, right))
            finally:
                _LocalFree(right)
        finally:
            _LocalFree(left)

    def is_owner_only(path: Path) -> bool:
        """DACL protegida, sem ACE herdada e com acesso concedido somente ao usuário atual."""
        sddl = read_dacl_sddl(path)
        if not sddl.startswith("D:"):
            return False
        flags = sddl[2 : sddl.find("(")] if "(" in sddl else sddl[2:]
        if "P" not in flags:
            return False
        aces = ACE_PATTERN.findall(sddl)
        if len(aces) != 1:
            return False
        fields = aces[0].split(";")
        if len(fields) < 6 or fields[0] != "A" or "ID" in fields[1]:
            return False
        return same_sid(fields[5], current_user_sid())
