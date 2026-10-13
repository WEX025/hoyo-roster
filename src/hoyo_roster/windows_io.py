"""Windows-only SID/DACL and non-reparse handles, for local fixed NTFS volumes.

Imported only on Windows. No chmod fallback and no privilege elevation.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import msvcrt
import ntsecuritycon
import pywintypes
import win32api
import win32con
import win32file
import win32security


def _sid():
    with win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
    ) as token:
        return win32security.GetTokenInformation(token, win32security.TokenUser)[0]


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
    if not drive or drive.startswith("\\\\"):
        raise OSError("UNSUPPORTED_PRIVATE_FILESYSTEM")
    root = drive + "\\"
    if win32file.GetDriveType(root) != win32con.DRIVE_FIXED:
        raise OSError("UNSUPPORTED_PRIVATE_FILESYSTEM")
    if win32api.GetVolumeInformation(root)[4].upper() != "NTFS":
        raise OSError("UNSUPPORTED_PRIVATE_FILESYSTEM")


def _descriptor(path: Path):
    return win32security.GetNamedSecurityInfo(
        str(path),
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
    return _private_descriptor(_descriptor(path))


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
    win32file.CreateDirectory(str(path), _security())
    if not private(path):
        raise OSError("PRIVATE_PERMISSIONS_FAILED")


def set_private(path: Path) -> None:
    descriptor = _security().SECURITY_DESCRIPTOR
    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        descriptor.GetSecurityDescriptorDacl(),
        None,
    )


def check_directory(path: Path) -> None:
    attributes = win32file.GetFileAttributes(str(path))
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
            str(path),
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
        str(source),
        str(destination),
        win32con.MOVEFILE_REPLACE_EXISTING | win32file.MOVEFILE_WRITE_THROUGH,
    )
