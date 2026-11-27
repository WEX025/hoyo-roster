"""Shared client session lifetime and request scheduling, with no browser ownership."""

from __future__ import annotations

import asyncio
import errno
from pathlib import Path

import portalocker

from . import paths, platform_io as io
from .client import ClientSession
from .config import Settings
from .errors import (
    AuthenticationRecoveryFailed,
    AuthenticationRequired,
    ClientSourceUnavailable,
    OperationError,
    WrongAccount,
    WorkspaceError,
    ClientProtocolError,
)
from .login import login
from .request_gate import RequestGate
from .storage.common import secure_private_directory, MetadataError

MODULE_ROOT = Path(__file__).resolve().parent


class ParallelRecoveryRequired(Exception):
    """Quiesce peer readers before entering shared login for this session version."""

    def __init__(self, version):
        super().__init__()
        self.version = version


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
    except (MetadataError, io.UnsafePath, OSError) as error:
        cause = error.__cause__ if isinstance(error, MetadataError) else error
        if isinstance(cause, OSError) and cause.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
            raise WorkspaceError("RUNTIME_NOT_WRITABLE") from None
        raise WorkspaceError("UNSAFE_PRIVATE_DIRECTORY") from None


class SessionService:
    """Borrow one attached client session. Never close it or create authentication files."""

    def __init__(self, client: ClientSession, root: Path | None = None):
        self.client = client
        self.root = root
        self.version = 0
        self.handoff = None
        # The host advances on human confirmation. version advances after identity verification.
        self.browser_generation = 0
        self._auth_lock = asyncio.Lock()
        self._pending_login = False
        self._recovery_failed = False
        self._checks = {}
        self._lock = None
        self.readiness = None
        self.parallel = False
        self._collection_login = False

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

    async def call_source(self, method, params, *, generation):
        if generation != self.browser_generation:
            raise AuthenticationRequired()
        result = await self.client.call(method, {**params, "generation": generation})
        if generation != self.browser_generation:
            raise AuthenticationRequired()
        return result

    async def identity(self, check, *, probe=None):
        """Let each game supply its official identity reader."""
        version = self.version
        try:
            try:
                return await check()
            except AuthenticationRequired:
                return await self.ensure_authenticated(
                    version, probe=probe or check, verify_current=check
                )
        except (OperationError, WorkspaceError) as error:
            if self.readiness is not None and self.version:
                error.stop_account = True
            raise

    async def collect(self, game, read):
        """Restart only this game's unfinished transaction after local verification."""
        start_version = self.version
        locally_restarted = False
        expected, check = self._checks[game]

        async def probe():
            if await check() != expected:
                raise WrongAccount()

        while True:
            version = self.version
            try:
                return await read()
            except AuthenticationRequired:
                await self.ensure_authenticated(
                    version, probe=probe, allow_login=version == start_version
                )
                if self.version == version and locally_restarted:
                    # The official identity is healthy but this source still denies access.
                    raise ClientSourceUnavailable() from None
                locally_restarted = self.version == version
                print("AUTH_RECOVERY=RESTART_CURRENT_GAME", flush=True)

    async def ensure_authenticated(
        self, observed_version, *, probe, verify_current=None, allow_login=True
    ):
        """One same-session probe. Only confirmed expiry can interrupt another game."""
        async with self._auth_lock:
            if self._recovery_failed:
                raise AuthenticationRecoveryFailed()
            if observed_version == self.version:
                if self._pending_login:
                    raise ParallelRecoveryRequired(observed_version)
                try:
                    return await probe()
                except AuthenticationRequired:
                    if not allow_login or self._collection_login:
                        raise AuthenticationRecoveryFailed() from None
                if self.parallel:
                    self._pending_login = True
                    raise ParallelRecoveryRequired(observed_version)
            return await self._login_or_reuse(observed_version, verify_current)

    async def resume_after_drain(self, observed_version, *, startup):
        if startup and observed_version == self.version and self.version > 0:
            raise AuthenticationRecoveryFailed()
        await self.recover(observed_version=observed_version)
        if not startup:
            self._collection_login = True

    async def recover(self, verify_current=None, *, observed_version=None):
        """Serialize the handoff and publish its version only after identities match."""
        version = self.version if observed_version is None else observed_version
        async with self._auth_lock:
            return await self._login_or_reuse(version, verify_current)

    async def _login_or_reuse(self, version, verify_current):
        if self._recovery_failed:
            raise AuthenticationRecoveryFailed()
        try:
            if version != self.version:
                return await verify_current() if verify_current is not None else None
            handoff = await login(self.client)
            if handoff.session is not self.client or not handoff.user_confirmed:
                raise ClientProtocolError()
            self.browser_generation += 1
            for expected, check in self._checks.values():
                if await check() != expected:
                    raise WrongAccount()
            current = await verify_current() if verify_current is not None else None
        except (OperationError, WorkspaceError) as error:
            self._recovery_failed = True
            error.stop_account = True
            raise
        except asyncio.CancelledError:
            self._recovery_failed = True
            raise
        except Exception:
            self._recovery_failed = True
            raise AuthenticationRecoveryFailed() from None
        self._pending_login = False
        self.handoff = handoff
        self.version = self.browser_generation
        return current

    def pin(self, game, roles, check):
        previous = self._checks.get(game)
        if previous is not None and previous[0] != roles:
            raise WrongAccount()
        self._checks[game] = (roles, check)
