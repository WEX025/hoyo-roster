"""Real local I/O plus explicit Windows policy mocks; mocks are not Windows acceptance."""

import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hoyo_roster import platform_io as io, paths
from hoyo_roster.storage.common import atomic_write_metadata, load_metadata


def test_process_lock_busy_then_release(tmp_path):
    lock = tmp_path / "workspace.lock"
    code = """import sys, portalocker
from pathlib import Path
from hoyo_roster.platform_io import exclusive_lock
try:
    with exclusive_lock(Path(sys.argv[1])):
        pass
except portalocker.exceptions.LockException:
    sys.exit(23)
"""
    with io.exclusive_lock(lock):
        result = subprocess.run([sys.executable, "-c", code, str(lock)], capture_output=True)
        assert result.returncode == 23
    result = subprocess.run([sys.executable, "-c", code, str(lock)], capture_output=True)
    assert result.returncode == 0


def test_unsafe_lock_does_not_modify_file(tmp_path):
    lock = tmp_path / "workspace.lock"
    lock.write_bytes(b"synthetic")
    lock.chmod(0o644)
    with pytest.raises(io.UnsafePath):
        with io.exclusive_lock(lock):
            pytest.fail("unsafe lock admitted")
    assert lock.read_bytes() == b"synthetic"
    assert stat.S_IMODE(lock.stat().st_mode) == 0o644


def test_hardlink_key_rejected(tmp_path):
    key = tmp_path / "key"
    key.write_bytes(b"synthetic")
    key.chmod(0o600)
    os.link(key, tmp_path / "other")
    with pytest.raises(io.UnsafePath):
        io.open_private(key, os.O_RDONLY)
    assert key.read_bytes() == b"synthetic"


def test_reparse_attribute_is_rejected_without_following():
    assert io.is_redirect(SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400))
    assert not io.is_redirect(SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0))


def test_local_file_url_uses_platform_converter(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "__file__", str(tmp_path / "site-packages/hoyo_roster/paths.py"))
    monkeypatch.setattr(
        paths.metadata,
        "distribution",
        lambda name: SimpleNamespace(
            read_text=lambda _: '{"url":"file:///C:/clone%20space","dir_info":{}}'
        ),
    )
    converter = Mock(return_value="C:/clone space")
    verify = Mock(return_value=tmp_path)
    monkeypatch.setattr(paths, "url2pathname", converter)
    monkeypatch.setattr(paths, "verified_repository", verify)
    assert paths.project_repository() == tmp_path
    converter.assert_called_once_with("/C:/clone%20space")
    verify.assert_called_once_with(Path("C:/clone space"))


def test_remote_file_url_not_used(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "__file__", str(tmp_path / "site-packages/hoyo_roster/paths.py"))
    monkeypatch.setattr(
        paths.metadata,
        "distribution",
        lambda name: SimpleNamespace(
            read_text=lambda _: '{"url":"file://remote/share","dir_info":{}}'
        ),
    )
    monkeypatch.setattr(paths, "verified_repository", lambda p: pytest.fail("remote URL accepted"))
    assert paths.project_repository() is None


@pytest.fixture
def windows(monkeypatch):
    # Import the Windows-only module against named, explicit API mocks. Do not
    # patch os.name: that changes pathlib globally and hides unsupported behavior.
    constants = dict(
        OBJECT_INHERIT_ACE=1,
        CONTAINER_INHERIT_ACE=2,
        INHERIT_ONLY_ACE=8,
        FILE_ALL_ACCESS=0x1F01FF,
        GENERIC_ALL=0x10000000,
        GENERIC_WRITE=0x40000000,
        GENERIC_READ=0x80000000,
        FILE_WRITE_DATA=2,
        FILE_APPEND_DATA=4,
        FILE_WRITE_EA=16,
        FILE_WRITE_ATTRIBUTES=256,
        DELETE=0x10000,
        WRITE_DAC=0x40000,
        WRITE_OWNER=0x80000,
        FILE_DELETE_CHILD=64,
        DRIVE_FIXED=3,
        FILE_ATTRIBUTE_REPARSE_POINT=0x400,
        FILE_ATTRIBUTE_DIRECTORY=0x10,
        CREATE_NEW=1,
        OPEN_ALWAYS=4,
        OPEN_EXISTING=3,
        FILE_SHARE_READ=1,
        FILE_SHARE_WRITE=2,
        FILE_FLAG_OPEN_REPARSE_POINT=0x200000,
        MOVEFILE_REPLACE_EXISTING=1,
        MOVEFILE_WRITE_THROUGH=8,
        TOKEN_QUERY=8,
    )
    security = SimpleNamespace(
        ACCESS_ALLOWED_ACE_TYPE=0,
        ACCESS_DENIED_ACE_TYPE=1,
        SE_FILE_OBJECT=1,
        OWNER_SECURITY_INFORMATION=1,
        DACL_SECURITY_INFORMATION=4,
        PROTECTED_DACL_SECURITY_INFORMATION=0x80000000,
        SE_DACL_PROTECTED=0x1000,
        ACL_REVISION=2,
        WinLocalSystemSid=22,
        WinBuiltinAdministratorsSid=26,
    )
    for name, module in {
        "msvcrt": SimpleNamespace(open_osfhandle=Mock()),
        "pywintypes": SimpleNamespace(error=type("WindowsErrorMock", (Exception,), {})),
        "win32api": SimpleNamespace(
            GetVolumeInformation=Mock(return_value=(None, None, None, None, "NTFS"))
        ),
        "win32con": SimpleNamespace(**constants),
        "ntsecuritycon": SimpleNamespace(**constants),
        "win32security": security,
        "win32file": SimpleNamespace(
            GetDriveType=Mock(return_value=3),
            MoveFileEx=Mock(),
            FILE_FLAG_OPEN_REPARSE_POINT=0x200000,
            MOVEFILE_WRITE_THROUGH=8,
        ),
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location(
        "windows_policy_mock", Path(io.__file__).with_name("windows_io.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_sid", lambda: "current")
    return module


class ACL:
    def __init__(self, entries):
        self.entries = entries

    def GetAceCount(self):
        return len(self.entries)

    def GetAce(self, index):
        return self.entries[index]


def descriptor(owner="current", entries=None):
    return SimpleNamespace(
        GetSecurityDescriptorOwner=lambda: owner,
        GetSecurityDescriptorDacl=lambda: None if entries is None else ACL(entries),
    )


@pytest.mark.parametrize(
    "owner,entries,expected",
    [
        ("current", [((0, 0), 0x1F01FF, "current")], True),
        ("foreign", [((0, 0), 0x1F01FF, "current")], False),
        ("current", None, False),
        ("current", [], False),
        ("current", [((0, 0), 0x1F01FF, "foreign")], False),
        ("current", [((0, 8), 0x1F01FF, "current")], False),
        ("current", [((0, 0), 1, "current")], False),
        ("current", [((1, 0), 0x1F01FF, "current")], False),
        ("current", [((0, 0), 0x1F01FF, "current"), ((0, 0), 1, "foreign")], False),
    ],
)
def test_windows_acl_policy_mock(windows, owner, entries, expected):
    assert windows._private_descriptor(descriptor(owner, entries)) is expected


@pytest.mark.parametrize("drive,filesystem", [("\\\\remote\\share", "NTFS"), ("C:", "FAT32")])
def test_windows_unsupported_volumes_mock(windows, drive, filesystem):
    windows.win32api.GetVolumeInformation.return_value = (None, None, None, None, filesystem)
    with pytest.raises(OSError, match="UNSUPPORTED_PRIVATE_FILESYSTEM"):
        windows.check_volume(PureWindowsPath(drive + "/private"))


def test_windows_write_through_failure_is_not_ignored_mock(windows):
    windows.win32file.MoveFileEx.side_effect = OSError("synthetic")
    with pytest.raises(OSError):
        windows.replace(Path("source"), Path("target"))
    assert windows.win32file.MoveFileEx.call_args.args[2] == 9


def test_windows_backend_dispatch_failure_preserves_old_mock(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    atomic_write_metadata(target, {"old": True})
    backend = SimpleNamespace(check_volume=Mock(), replace=Mock(side_effect=OSError("synthetic")))
    # Only publication dispatch is mocked. Creation/flush use real POSIX handles.
    original = io.replace

    def fail(source, destination):
        with monkeypatch.context() as local:
            local.setattr(io, "WINDOWS", True)
            local.setattr(io, "backend", backend, raising=False)
            original(source, destination)

    monkeypatch.setattr(io, "replace", fail)
    with pytest.raises(OSError):
        atomic_write_metadata(target, {"new": True})
    assert load_metadata(target) == {"old": True}


@pytest.mark.parametrize(
    "attributes,links,owner",
    [(0x400, 1, "current"), (0x10, 1, "current"), (0, 2, "current"), (0, 1, "foreign")],
)
def test_windows_open_rejects_unsafe_handle_mock(windows, monkeypatch, attributes, links, owner):
    handle = SimpleNamespace(Close=Mock(), Detach=Mock())
    windows.win32file.CreateFile = Mock(return_value=handle)
    windows.win32file.GetFileInformationByHandle = Mock(
        return_value=(attributes, 0, 0, 0, 0, 0, 0, links, 0, 0)
    )
    windows.win32security.GetSecurityInfo = Mock(
        return_value=descriptor(owner, [((0, 0), 0x1F01FF, "current")])
    )
    monkeypatch.setattr(windows, "_security", Mock())
    with pytest.raises(OSError, match="UNSAFE_PRIVATE_FILE"):
        windows.open_private(Path("synthetic"), os.O_RDONLY)
    assert windows.win32file.CreateFile.call_args.args[5] == 0x200000
    assert windows.win32file.CreateFile.call_args.args[2] == 3  # no share-delete
    handle.Close.assert_called_once()
    handle.Detach.assert_not_called()


def test_existing_insecure_browser_profile_preserved(tmp_path):
    from hoyo_roster.games.genshin.browser import resolve_browser_profile
    from hoyo_roster.errors import ConfigurationError
    from hoyo_roster.storage.common import MetadataError

    profile = tmp_path / "profile"
    profile.mkdir(mode=0o755)
    with pytest.raises((ConfigurationError, MetadataError)):
        resolve_browser_profile(tmp_path / "contracts", profile)
    assert stat.S_IMODE(profile.stat().st_mode) == 0o755


def test_windows_open_transfers_verified_handle_mock(windows, monkeypatch):
    class Handle:
        Close = Mock()
        Detach = Mock()

        def __int__(self):
            return 77

    handle = Handle()
    windows.win32file.CreateFile = Mock(return_value=handle)
    windows.win32file.GetFileInformationByHandle = Mock(return_value=(0, 0, 0, 0, 0, 0, 0, 1, 0, 0))
    windows.win32security.GetSecurityInfo = Mock(
        return_value=descriptor("current", [((0, 0), 0x1F01FF, "current")])
    )
    windows.msvcrt.open_osfhandle.return_value = 88
    monkeypatch.setattr(windows, "_security", Mock())
    monkeypatch.setattr(windows.os, "O_BINARY", 0x8000, raising=False)
    monkeypatch.setattr(windows.os, "fstat", lambda fd: SimpleNamespace(st_mode=stat.S_IFREG))
    assert windows.open_private(Path("synthetic"), os.O_CREAT | os.O_RDWR) == 88
    assert windows.win32file.CreateFile.call_args.args[1] == 0xC0000000
    assert windows.win32file.CreateFile.call_args.args[4] == 4
    windows.msvcrt.open_osfhandle.assert_called_once_with(77, os.O_RDWR | 0x8000)
    handle.Detach.assert_called_once()
