"""Shared client session lifetime and request scheduling, with no browser ownership."""

from __future__ import annotations

from pathlib import Path

import portalocker

from . import paths, platform_io as io
from .client import ClientSession
from .config import Settings
from .errors import (
    AuthenticationRecoveryFailed,
    OperationError,
    WrongAccount,
    WorkspaceError,
    ClientProtocolError,
)
from .login import login
from .request_gate import RequestGate
from .storage.common import secure_private_directory, MetadataError

MODULE_ROOT = Path(__file__).resolve().parent


def runtime_scope(value: Path | None = None) -> Path:
    root = (value if value is not None else paths.default_private_root()).expanduser()
    if not root.is_absolute():
        raise WorkspaceError("INVALID_RUNTIME_ROOT")
    if io.WINDOWS and str(root).startswith("\\\\"):
        raise WorkspaceError("UNSUPPORTED_PRIVATE_FILESYSTEM")
    try:
        io.check_components(root)
        resolved = root.resolve()
        repository = paths.project_repository()
        if (
            paths.containing_repository(resolved) is not None
            or (repository is not None and repository.is_relative_to(resolved))
            or resolved.is_relative_to(MODULE_ROOT)
            or MODULE_ROOT.is_relative_to(resolved)
        ):
            raise WorkspaceError("UNSAFE_RUNTIME_ROOT")
        return secure_private_directory(secure_private_directory(root) / "client-runtime-v1")
    except (MetadataError, io.UnsafePath, OSError):
        raise WorkspaceError("UNSAFE_PRIVATE_DIRECTORY") from None


class SessionService:
    """Borrow one attached client session. Never close it or create authentication files."""

    def __init__(self, client: ClientSession, root: Path | None = None):
        self.client = client
        self.root = root
        self.version = 0
        self.handoff = None
        self._checks = {}
        self._lock = None

    async def __aenter__(self):
        # Prove a real host connection before selecting any private runtime paths.
        await self.client.attach()
        self.scope = runtime_scope(self.root)
        self.gate = RequestGate(Settings(external_root=self.scope))
        self._lock = io.exclusive_lock(self.scope / "session.lock")
        try:
            self._lock.__enter__()
        except portalocker.exceptions.LockException:
            raise WorkspaceError("USER_WORKSPACE_BUSY") from None
        return self

    async def __aexit__(self, *exc):
        # The host owns browser/page retention even on cancellation or failure.
        self._lock.__exit__(*exc)

    async def recover(self, verify_current=None):
        try:
            handoff = await login(self.client)
            if handoff.session is not self.client or not handoff.user_confirmed:
                raise ClientProtocolError()
            # These callbacks are game-owned verification, after the login module returned.
            for expected, check in self._checks.values():
                if await check() != expected:
                    raise WrongAccount()
            current = await verify_current() if verify_current is not None else None
        except (OperationError, WorkspaceError) as error:
            # Failure here invalidates the shared recovery, even if the same code is
            # recoverable when raised by an ordinary per-game source read.
            error.stop_account = True
            raise
        except Exception:
            raise AuthenticationRecoveryFailed() from None
        # Publish the recovered session version only after all pinned checks pass.
        self.handoff = handoff
        self.version += 1
        return current

    def pin(self, game, roles, check):
        previous = self._checks.get(game)
        if previous is not None and previous[0] != roles:
            raise WrongAccount()
        self._checks[game] = (roles, check)
