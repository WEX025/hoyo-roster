"""Small local-filesystem boundary; game rules and transaction policy live above it."""

from __future__ import annotations

import os
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterator

import portalocker

WINDOWS = os.name == "nt"
if WINDOWS:
    from . import windows_io as backend


class UnsafePath(ValueError):
    """A fixed, privacy-safe filesystem rejection."""


def filesystem_path(path: Path) -> Path:
    """Use the local extended namespace internally, without resolving redirects."""
    path = path.expanduser().absolute()
    return backend.extended_path(path) if WINDOWS else path


def is_redirect(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & 0x400
    )


def check_components(path: Path) -> None:
    # Check the lexical path BEFORE resolve can erase a junction/symlink.
    path = path.expanduser().absolute()
    if WINDOWS:
        backend.check_volume(path)
    for part in (*reversed(path.parents), path):
        if os.path.lexists(part) and is_redirect(part.lstat()):
            raise UnsafePath("UNSAFE_REDIRECTED_PATH")


def owned(path: Path) -> bool:
    metadata = path.lstat()
    if is_redirect(metadata):
        return False
    return backend.owned(path) if WINDOWS else metadata.st_uid == os.getuid()


def private(path: Path, mode: int | None = None) -> bool:
    metadata = path.lstat()
    if is_redirect(metadata) or not (
        stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)
    ):
        return False
    mode = mode if mode is not None else (0o700 if stat.S_ISDIR(metadata.st_mode) else 0o600)
    if WINDOWS:
        return backend.private(path)
    return metadata.st_uid == os.getuid() and stat.S_IMODE(metadata.st_mode) == mode


def repository_container_safe(path: Path) -> bool:
    check_components(path)
    if WINDOWS:
        # The public clone container may inherit ordinary read access, but no other
        # non-system identity may write it. Private children have protected DACLs.
        return backend.container_safe(path)
    metadata = path.lstat()
    return stat.S_ISDIR(metadata.st_mode) and owned(path) and not metadata.st_mode & 0o022


def mkdir_private(path: Path) -> None:
    check_components(path.parent)
    if WINDOWS:
        backend.mkdir_private(path)
    else:
        path.mkdir(mode=0o700)
        path.chmod(0o700)


def set_private(path: Path, mode: int | None = None) -> None:
    """Only for newly generated application objects, never existing user roots."""
    check_components(path)
    if not owned(path):
        raise UnsafePath("UNSAFE_OWNER")
    if WINDOWS:
        backend.set_private(path)
    else:
        path.chmod(mode if mode is not None else (0o700 if path.is_dir() else 0o600))
    if not private(path, mode):
        raise UnsafePath("PRIVATE_PERMISSIONS_FAILED")


def open_private(path: Path, flags: int) -> int:
    check_components(path)
    if WINDOWS:
        return backend.open_private(path, flags)
    descriptor = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    try:
        metadata = os.fstat(descriptor)
        entry = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or (entry.st_dev, entry.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise UnsafePath("UNSAFE_PRIVATE_FILE")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    descriptor = open_private(path, os.O_CREAT | os.O_RDWR)
    with os.fdopen(descriptor, "r+b", buffering=0) as stream:
        acquired = False
        try:
            portalocker.lock(stream, portalocker.LOCK_EX | portalocker.LOCK_NB)
            acquired = True
            yield
        finally:
            if acquired:
                portalocker.unlock(stream)


def read_regular(path: Path, *, require_private: bool = False) -> bytes:
    check_components(path)
    if require_private:
        descriptor = open_private(path, os.O_RDONLY)
    elif WINDOWS:
        descriptor = backend.open_private(path, os.O_RDONLY, require_private=False)
    else:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            metadata = os.fstat(descriptor)
            entry = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or (entry.st_dev, entry.st_ino) != (metadata.st_dev, metadata.st_ino)
            ):
                raise UnsafePath("UNSAFE_REGULAR_FILE")
        except BaseException:
            os.close(descriptor)
            raise
    with os.fdopen(descriptor, "rb") as stream:
        return stream.read()


def temporary_file(path: Path, prefix: str) -> tuple[int, Path]:
    temporary = path.parent / f"{prefix}{uuid.uuid4()}.tmp"
    return open_private(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY), temporary


def sync_file(descriptor: int) -> None:
    os.fsync(descriptor)


def replace(source: Path, destination: Path) -> None:
    check_components(source)
    check_components(destination)
    if WINDOWS:
        backend.replace(source, destination)
    else:
        os.replace(source, destination)


def sync_directory(path: Path) -> None:
    check_components(path)
    if WINDOWS:
        # Win32 has no supported unprivileged directory-fsync equivalent. Each
        # publication uses MoveFileEx WRITE_THROUGH; see docs/platform-support.md.
        # Never swallow an attempted file flush or replace failure.
        backend.check_directory(path)
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
