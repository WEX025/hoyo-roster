"""Windows-only SID/DACL and non-reparse handles, for local fixed NTFS volumes.

Imported only on Windows. No chmod fallback and no privilege elevation.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import msvcrt
import ntsecuritycon
import pywintypes
import win32api
import win32con
import win32file
import win32process
import win32security


def _check_asyncio_compatibility():
    """Refuse an unreviewed private asyncio ABI before allocating any handles.

    Fingerprints are normalized ASTs from CPython 3.12.14. Patch releases with
    identical contracts are accepted; edits, missing source, other versions or
    implementations require explicit review plus the native contract tests.
    """
    import ast
    import hashlib
    import inspect
    import textwrap
    from asyncio import proactor_events, windows_events
    from .errors import WorkspaceError

    expected = {
        "BaseProactorEventLoop.__init__": "8502c6ea7b7f920563fa80af5626ab8de8db6c3a449d240f2eee9b3b2079ed77",
        "BaseProactorEventLoop.close": "2fd286c7656d443b144d4d919cf54fd8496ba335cdca1fa7705cfc22891121d2",
        "BaseProactorEventLoop._make_self_pipe": "1f130b7849233216615c60406f1b5fffd63aec5b69d4669c2e7720473610ebd9",
        "BaseProactorEventLoop._close_self_pipe": "51235f1f17087ae900ab52fcb72d3bea72421e14046e4f9bdc18bebfef164eba",
        "BaseProactorEventLoop._loop_self_reading": "97c3ae3c2c89febd5a62799ff76e3a15c314a6f5d902e0a23a91f3a879aa7749",
        "ProactorEventLoop.__init__": "aedf8de6b88c6db755cbdde12b09bbfb94d5f3aa034e1279c84ded6c46f40020",
        "ProactorEventLoop.run_forever": "3dc0de76b3a3f5121825fb70f73d136e2dfbda07c7b937eef5ed4032812d1305",
        "IocpProactor.recv": "b120427b0a394a635fe6b7f5c1eaebc13abd4d1cf201b5d7457c06eb6fb491e3",
    }
    try:
        if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 12):
            raise ValueError()
        classes = {
            "BaseProactorEventLoop": proactor_events.BaseProactorEventLoop,
            "ProactorEventLoop": windows_events.ProactorEventLoop,
            "IocpProactor": windows_events.IocpProactor,
        }
        for qualified, digest in expected.items():
            owner, method = qualified.split(".")
            source = inspect.getsource(getattr(classes[owner], method))
            normalized = ast.dump(ast.parse(textwrap.dedent(source)), include_attributes=False)
            if hashlib.sha256(normalized.encode()).hexdigest() != digest:
                raise ValueError()
    except (AttributeError, OSError, TypeError, ValueError, SyntaxError):
        raise WorkspaceError("WINDOWS_ASYNCIO_UNSUPPORTED_RUNTIME") from None


def new_event_loop():
    """Python 3.12 IOCP loop with a local pipe instead of a TCP wakeup socket."""
    import asyncio
    import _winapi
    import win32pipe
    from asyncio.windows_utils import PipeHandle, pipe
    from asyncio.windows_events import IocpProactor

    _check_asyncio_compatibility()

    class PipeProactor(IocpProactor):
        def select(self, timeout=None):
            # Windows set_wakeup_fd requires a socket. Bound IOCP's idle wait
            # instead, so Runner's normal SIGINT cancellation remains prompt.
            return super().select(0.1 if timeout is None else min(timeout, 0.1))

    class PipeEventLoop(asyncio.ProactorEventLoop):
        def __init__(self):
            # CPython 3.12's Proactor constructor/close install a socket-only
            # signal fd. Initialize the same IOCP state without changing any
            # process-global signal fd or event-loop policy.
            asyncio.BaseEventLoop.__init__(self)
            self._proactor = self._selector = PipeProactor()
            self._self_reading_future = None
            self._accept_futures = {}
            self._proactor.set_loop(self)
            self._make_self_pipe()

        def close(self):
            if self.is_running():
                raise RuntimeError("Cannot close a running event loop")
            if self.is_closed():
                return
            self._stop_accept_futures()
            self._close_self_pipe()
            self._proactor.close()
            self._proactor = self._selector = None
            asyncio.BaseEventLoop.close(self)

        def _make_self_pipe(self):
            reader, writer = pipe(overlapped=(True, False))
            self._ssock, self._csock = PipeHandle(reader), PipeHandle(writer)
            try:
                # A full wakeup buffer already guarantees a pending wakeup.
                # Never block the notifying thread while writing another byte.
                win32pipe.SetNamedPipeHandleState(writer, win32pipe.PIPE_NOWAIT, None, None)
            except BaseException:
                self._ssock.close()
                self._csock.close()
                raise
            self._internal_fds += 1

        def _write_to_self(self):
            writer = self._csock
            if writer is not None:
                try:
                    _winapi.WriteFile(writer.fileno(), b"\0")
                except (OSError, ValueError):
                    # Same close/full-buffer race as the standard self socket.
                    pass

    loop = PipeEventLoop()
    asyncio.set_event_loop(loop)
    return loop


def _process_parent(handle) -> int:
    # The documented ProcessBasicInformation layout is pointer-sized on both
    # Windows architectures. Query this exact open handle, not a guessed PID.
    import ctypes
    from ctypes import wintypes

    class BasicInformation(ctypes.Structure):
        _fields_ = [
            ("reserved1", ctypes.c_void_p),
            ("peb", ctypes.c_void_p),
            ("reserved2", ctypes.c_void_p * 2),
            ("pid", ctypes.c_size_t),
            ("parent", ctypes.c_size_t),
        ]

    query = ctypes.WinDLL("ntdll").NtQueryInformationProcess
    query.argtypes = [
        wintypes.HANDLE,
        wintypes.ULONG,
        ctypes.c_void_p,
        wintypes.ULONG,
        ctypes.POINTER(wintypes.ULONG),
    ]
    query.restype = wintypes.LONG
    info, length = BasicInformation(), wintypes.ULONG()
    if query(int(handle), 0, ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(length)) < 0:
        raise OSError("PROCESS_PARENT_UNAVAILABLE")
    if length.value != ctypes.sizeof(info) or info.parent <= 0:
        raise OSError("PROCESS_PARENT_UNAVAILABLE")
    return info.parent


def _process_image(handle) -> Path:
    import ctypes
    from ctypes import wintypes

    query = ctypes.WinDLL("kernel32", use_last_error=True).QueryFullProcessImageNameW
    query.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    query.restype = wintypes.BOOL
    buffer, length = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
    if not query(int(handle), 0, buffer, ctypes.byref(length)):
        raise OSError("PROCESS_PARENT_UNAVAILABLE")
    return Path(buffer.value).resolve()


def launch_parent_pid() -> int:
    """Skip exactly one verified CPython venv launcher; never search ancestors."""
    parent = os.getppid()
    if sys.prefix == sys.base_prefix:
        return parent
    try:
        handle = win32api.OpenProcess(0x1000, False, parent)
        try:
            if _process_image(handle) != Path(sys.executable).resolve():
                return parent  # A direct interpreter launch has no redirector.
            if win32process.GetExitCodeProcess(handle) != 259:
                raise OSError("PROCESS_PARENT_UNAVAILABLE")
            created = win32process.GetProcessTimes(handle)["CreationTime"]
            if created > win32process.GetProcessTimes(win32api.GetCurrentProcess())["CreationTime"]:
                raise OSError("PROCESS_PARENT_UNAVAILABLE")
            controller = _process_parent(handle)
            owner = win32api.OpenProcess(0x1000, False, controller)
            try:
                if (
                    win32process.GetExitCodeProcess(owner) != 259
                    or win32process.GetProcessTimes(owner)["CreationTime"] > created
                ):
                    raise OSError("PROCESS_PARENT_UNAVAILABLE")
            finally:
                owner.Close()
            return controller
        finally:
            handle.Close()
    except pywintypes.error:
        raise OSError("PROCESS_PARENT_UNAVAILABLE") from None


def process_alive(pid: int) -> bool:
    # Windows signal zero is not a POSIX-style existence probe. Query a handle;
    # never signal or terminate a process while deciding whether recovery is safe.
    try:
        handle = win32api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    except pywintypes.error as error:
        if error.winerror == 87:  # ERROR_INVALID_PARAMETER: no such PID
            return False
        if error.winerror == 5:  # Access denied is not evidence of a dead owner.
            return True
        raise OSError("PROCESS_STATE_UNAVAILABLE") from None
    try:
        return win32process.GetExitCodeProcess(handle) == 259  # STILL_ACTIVE
    finally:
        handle.Close()


def _native_path(path: Path) -> str:
    # pywin32 APIs do not inherit Python's long-path handling. Keep the validated
    # lexical path (never resolve through a reparse point) and extend local paths.
    value = str(path)
    if len(value) < 240:
        return value
    return str(extended_path(path))


def extended_path(path: Path) -> Path:
    value = str(path)
    if value.startswith("\\\\?\\") and len(path.drive) == 6 and path.drive[5] == ":":
        return path
    if not path.is_absolute() or len(path.drive) != 2 or path.drive[1] != ":":
        raise OSError("UNSUPPORTED_PRIVATE_FILESYSTEM")
    if any(part.endswith((" ", ".")) or ":" in part for part in path.parts[1:]):
        raise OSError("UNSUPPORTED_PRIVATE_FILESYSTEM")
    return Path("\\\\?\\" + value)


def _sid():
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        return win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()


def _security():
    sid = _sid()
    acl = win32security.ACL()
    acl.AddAccessAllowedAceEx(
        win32security.ACL_REVISION,
        win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE,
        ntsecuritycon.FILE_ALL_ACCESS,
        sid,
    )
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorOwner(sid, False)
    descriptor.SetSecurityDescriptorDacl(True, acl, False)
    descriptor.SetSecurityDescriptorControl(
        win32security.SE_DACL_PROTECTED, win32security.SE_DACL_PROTECTED
    )
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    return attributes


def check_volume(path: Path) -> None:
    # UNC, removable/FAT/exFAT/network/cloud redirected trees are deliberately out
    # of scope; DACL and rename assumptions require a local fixed NTFS volume.
    drive = path.drive
    if drive.startswith("\\\\?\\") and len(drive) == 6 and drive[5] == ":":
        drive = drive[4:]
    if not drive or drive.startswith("\\\\"):
        raise OSError("UNSUPPORTED_PRIVATE_FILESYSTEM")
    root = drive + "\\"
    if win32file.GetDriveType(root) != win32con.DRIVE_FIXED:
        raise OSError("UNSUPPORTED_PRIVATE_FILESYSTEM")
    if win32api.GetVolumeInformation(root)[4].upper() != "NTFS":
        raise OSError("UNSUPPORTED_PRIVATE_FILESYSTEM")


def _descriptor(path: Path):
    return win32security.GetNamedSecurityInfo(
        _native_path(path),
        win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
    )


def owned(path: Path) -> bool:
    return _descriptor(path).GetSecurityDescriptorOwner() == _sid()


def _private_descriptor(descriptor) -> bool:
    sid = _sid()
    if descriptor.GetSecurityDescriptorOwner() != sid:
        return False
    acl = descriptor.GetSecurityDescriptorDacl()
    if acl is None or acl.GetAceCount() == 0:
        return False
    allowed = 0
    for index in range(acl.GetAceCount()):
        ace = acl.GetAce(index)
        if ace[0][0] != win32security.ACCESS_ALLOWED_ACE_TYPE or ace[2] != sid:
            return False
        if not ace[0][1] & win32con.INHERIT_ONLY_ACE:
            allowed |= ace[1]
    return allowed & ntsecuritycon.FILE_ALL_ACCESS == ntsecuritycon.FILE_ALL_ACCESS


def private(path: Path) -> bool:
    try:
        return _private_descriptor(_descriptor(path))
    except pywintypes.error:
        # A prior desktop sandbox SID may own a preserved private directory.
        # Treat an unreadable DACL as unsafe, never as a generic task failure.
        return False


def container_safe(path: Path) -> bool:
    if not path.is_dir() or not owned(path):
        return False
    acl = _descriptor(path).GetSecurityDescriptorDacl()
    if acl is None:
        return False
    trusted = (
        _sid(),
        win32security.CreateWellKnownSid(win32security.WinLocalSystemSid),
        win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid),
    )
    writes = (
        win32con.GENERIC_ALL
        | win32con.GENERIC_WRITE
        | ntsecuritycon.FILE_WRITE_DATA
        | ntsecuritycon.FILE_APPEND_DATA
        | ntsecuritycon.FILE_WRITE_EA
        | ntsecuritycon.FILE_WRITE_ATTRIBUTES
        | win32con.DELETE
        | win32con.WRITE_DAC
        | win32con.WRITE_OWNER
        | ntsecuritycon.FILE_DELETE_CHILD
    )
    for index in range(acl.GetAceCount()):
        ace = acl.GetAce(index)
        if ace[0][1] & win32con.INHERIT_ONLY_ACE:
            continue
        if ace[0][0] == win32security.ACCESS_DENIED_ACE_TYPE:
            continue
        if ace[0][0] != win32security.ACCESS_ALLOWED_ACE_TYPE:
            return False
        if ace[2] not in trusted and ace[1] & writes:
            return False
    return True


def mkdir_private(path: Path) -> None:
    win32file.CreateDirectory(_native_path(path), _security())
    if not private(path):
        raise OSError("PRIVATE_PERMISSIONS_FAILED")


def set_private(path: Path) -> None:
    descriptor = _security().SECURITY_DESCRIPTOR
    win32security.SetNamedSecurityInfo(
        _native_path(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        descriptor.GetSecurityDescriptorDacl(),
        None,
    )


def check_directory(path: Path) -> None:
    attributes = win32file.GetFileAttributes(_native_path(path))
    if (
        attributes & win32con.FILE_ATTRIBUTE_REPARSE_POINT
        or not attributes & win32con.FILE_ATTRIBUTE_DIRECTORY
    ):
        raise OSError("UNSAFE_DIRECTORY")


def open_private(path: Path, flags: int, *, require_private: bool = True) -> int:
    access = win32con.GENERIC_READ
    if flags & (os.O_WRONLY | os.O_RDWR):
        access = win32con.GENERIC_WRITE | (win32con.GENERIC_READ if flags & os.O_RDWR else 0)
    creation = (
        (win32con.CREATE_NEW if flags & os.O_EXCL else win32con.OPEN_ALWAYS)
        if flags & os.O_CREAT
        else win32con.OPEN_EXISTING
    )
    try:
        handle = win32file.CreateFile(
            _native_path(path),
            access,
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
            _security(),
            creation,
            win32file.FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
    except pywintypes.error as error:
        if error.winerror in (80, 183):
            raise FileExistsError("PRIVATE_FILE_EXISTS") from None
        raise OSError("PRIVATE_OPEN_FAILED") from None
    try:
        info = win32file.GetFileInformationByHandle(handle)
        descriptor = win32security.GetSecurityInfo(
            handle,
            win32security.SE_FILE_OBJECT,
            win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
        )
        if (
            info[0] & (win32con.FILE_ATTRIBUTE_REPARSE_POINT | win32con.FILE_ATTRIBUTE_DIRECTORY)
            or info[7] != 1
            or (require_private and not _private_descriptor(descriptor))
        ):
            raise OSError("UNSAFE_PRIVATE_FILE")
        descriptor_fd = msvcrt.open_osfhandle(
            int(handle), flags & (os.O_RDONLY | os.O_WRONLY | os.O_RDWR) | os.O_BINARY
        )
        handle.Detach()
        if not stat.S_ISREG(os.fstat(descriptor_fd).st_mode):
            os.close(descriptor_fd)
            raise OSError("UNSAFE_PRIVATE_FILE")
        return descriptor_fd
    finally:
        handle.Close()


def replace(source: Path, destination: Path) -> None:
    win32file.MoveFileEx(
        _native_path(source),
        _native_path(destination),
        win32con.MOVEFILE_REPLACE_EXISTING | win32file.MOVEFILE_WRITE_THROUGH,
    )
