"""Private filesystem primitives and cross-process advisory locks."""

from __future__ import annotations

import fcntl
import os
import stat
from pathlib import Path
from types import TracebackType

from .errors import ExternalStateUnsafe

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def _assert_owner(metadata: os.stat_result) -> None:
    if metadata.st_uid != os.getuid():
        raise ExternalStateUnsafe(
            "external state is not owned by the current user"
        )


def ensure_safe_parent_directory(path: Path) -> Path:
    """Allow current-user container directories that group/other cannot write."""

    absolute = path.expanduser().absolute()
    try:
        if not os.path.lexists(absolute):
            absolute.mkdir(parents=True, mode=PRIVATE_DIRECTORY_MODE, exist_ok=True)
        metadata = absolute.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ExternalStateUnsafe(
                "external parent directory type is unsafe"
            )
        _assert_owner(metadata)
        if stat.S_IMODE(metadata.st_mode) & 0o022:
            raise ExternalStateUnsafe(
                "external parent directory permissions are unsafe"
            )
    except ExternalStateUnsafe:
        raise
    except OSError as error:
        raise ExternalStateUnsafe(
            "external parent directory could not be secured"
        ) from error
    return absolute.resolve(strict=True)


def ensure_private_directory(path: Path) -> Path:
    """Create one private directory or reject unsafe existing state."""

    absolute = path.expanduser().absolute()
    try:
        if os.path.lexists(absolute):
            metadata = absolute.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ExternalStateUnsafe("external directory type is unsafe")
            _assert_owner(metadata)
            if stat.S_IMODE(metadata.st_mode) != PRIVATE_DIRECTORY_MODE:
                raise ExternalStateUnsafe(
                    "external directory permissions are unsafe"
                )
        else:
            absolute.mkdir(parents=True, mode=PRIVATE_DIRECTORY_MODE, exist_ok=True)
            metadata = absolute.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ExternalStateUnsafe(
                    "external directory creation was unsafe"
                )
            _assert_owner(metadata)
            if stat.S_IMODE(metadata.st_mode) != PRIVATE_DIRECTORY_MODE:
                raise ExternalStateUnsafe(
                    "external directory permissions are unsafe"
                )
    except ExternalStateUnsafe:
        raise
    except OSError as error:
        raise ExternalStateUnsafe(
            "external directory could not be secured"
        ) from error
    return absolute.resolve(strict=True)


def ensure_private_file(path: Path) -> Path:
    """Create one private regular file or reject unsafe existing state."""

    absolute = path.expanduser().absolute()
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(absolute, flags, PRIVATE_FILE_MODE)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ExternalStateUnsafe("external file type is unsafe")
            _assert_owner(metadata)
            if stat.S_IMODE(metadata.st_mode) != PRIVATE_FILE_MODE:
                raise ExternalStateUnsafe(
                    "external file permissions are unsafe"
                )
        finally:
            os.close(descriptor)
        path_metadata = absolute.lstat()
        if stat.S_ISLNK(path_metadata.st_mode) or not stat.S_ISREG(path_metadata.st_mode):
            raise ExternalStateUnsafe("external file type is unsafe")
    except ExternalStateUnsafe:
        raise
    except OSError as error:
        raise ExternalStateUnsafe(
            "external file could not be secured"
        ) from error
    return absolute.resolve(strict=True)


class FileLock:
    """An exclusive advisory lock whose file is never deleted for recovery."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._descriptor: int | None = None

    def __repr__(self) -> str:
        state = "held" if self._descriptor is not None else "free"
        return f"FileLock(state={state!r})"

    @property
    def held(self) -> bool:
        return self._descriptor is not None

    def acquire(self, *, blocking: bool = True) -> bool:
        if self._descriptor is not None:
            raise RuntimeError("lock is already held")
        safe_path = ensure_private_file(self._path)
        flags = os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(safe_path, flags)
        operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(descriptor, operation)
        except BlockingIOError:
            os.close(descriptor)
            return False
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        return True

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> FileLock:
        if not self.acquire():
            raise RuntimeError("blocking lock acquisition unexpectedly failed")
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


def is_lock_held(path: Path) -> bool:
    lock = FileLock(path)
    acquired = lock.acquire(blocking=False)
    if acquired:
        lock.release()
        return False
    return True
