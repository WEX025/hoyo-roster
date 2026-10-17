"""Explicit composition for synthetic stores."""

import importlib
from hoyo_roster import platform_io as io


def candidate_policy_for(game):
    return importlib.import_module(f"hoyo_roster.games.{game}.candidate").candidate_policy()


def make_shared(path, mode=0o755):
    """Make a newly generated synthetic fixture insecure on its actual OS."""
    if not io.WINDOWS:
        path.chmod(mode)
        return
    native = io.backend
    security = native.win32security
    acl = native._security().SECURITY_DESCRIPTOR.GetSecurityDescriptorDacl()
    inheritance = (
        native.win32con.OBJECT_INHERIT_ACE | native.win32con.CONTAINER_INHERIT_ACE
        if path.is_dir()
        else 0
    )
    acl.AddAccessAllowedAceEx(
        security.ACL_REVISION,
        inheritance,
        native.ntsecuritycon.FILE_GENERIC_READ,
        security.CreateWellKnownSid(security.WinWorldSid),
    )
    security.SetNamedSecurityInfo(
        native._native_path(path),
        security.SE_FILE_OBJECT,
        security.DACL_SECURITY_INFORMATION | security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        acl,
        None,
    )
    assert not io.private(path)
