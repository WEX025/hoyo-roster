"""Account-owned, lazy shared browser session. No game or business-store imports."""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import portalocker
from playwright.async_api import async_playwright

from . import paths, platform_io as io
from .browser_runtime import launch_profile, profile_in_use
from .config import Settings
from .errors import ContextNotReusable, WrongAccount
from .login import coordinate_login
from .models import OwnedRole
from .request_gate import RequestGate
from .storage.common import secure_private_directory, remove_owned_tree
from .browser import operation_browser
from .errors import WorkspaceError
from .storage.common import MetadataError

MODULE_ROOT = Path(__file__).resolve().parent


def account_scope(value: Path | None = None) -> Path:
    """A fresh account namespace; never look for or migrate old identity profiles."""
    root = (value if value is not None else paths.default_private_root()).expanduser()
    if not root.is_absolute():
        raise WorkspaceError("INVALID_AUTH_ROOT")
    if io.WINDOWS and str(root).startswith("\\\\"):
        raise WorkspaceError("UNSUPPORTED_PRIVATE_FILESYSTEM")
    try:
        io.check_components(root)
    except (io.UnsafePath, OSError):
        raise WorkspaceError("UNSAFE_PRIVATE_DIRECTORY") from None
    resolved = root.resolve()
    repository = paths.project_repository()
    if (
        paths.containing_repository(resolved) is not None
        or (repository is not None and repository.is_relative_to(resolved))
        or resolved.is_relative_to(MODULE_ROOT)
        or MODULE_ROOT.is_relative_to(resolved)
    ):
        raise WorkspaceError("UNSAFE_AUTH_ROOT")
    try:
        return secure_private_directory(secure_private_directory(root) / "account-session-v1")
    except MetadataError:
        raise WorkspaceError("UNSAFE_PRIVATE_DIRECTORY") from None


@asynccontextmanager
async def account_browser(profile: Path, *, headed: bool):
    profile = secure_private_directory(profile)
    playwright = await async_playwright().start()
    context = None
    try:
        context = await launch_profile(
            playwright, profile, headed=headed, args=("--restore-last-session",)
        )
        # Stale restored pages must not leave other games' natural requests running.
        for page in tuple(context.pages):
            await page.close()
        yield context
    finally:
        try:
            if context is not None:
                await context.close()
        finally:
            await playwright.stop()


class AuthService:
    """One exclusive account Profile/context for an entire selected-game task."""

    def __init__(self, root: Path | None, *, headed: bool, timeout: float):
        self.scope = account_scope(root)
        self.profile = self.scope / "browser-profile"
        self.headed, self.timeout = headed, timeout
        self.gate = RequestGate(Settings(external_root=self.scope))
        self.context = None
        self.version = 0
        self._stack = None
        self._lock = None
        self._owned = {}

    async def __aenter__(self):
        self._lock = io.exclusive_lock(self.scope / "account.lock")
        try:
            self._lock.__enter__()
        except portalocker.exceptions.LockException:
            raise WorkspaceError("USER_WORKSPACE_BUSY") from None
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        return self

    async def __aexit__(self, *exc):
        try:
            return await self._stack.__aexit__(*exc)
        finally:
            self.context = None
            # The lock remains held until the real context close attempt completes.
            self._lock.__exit__(*exc)
            self._stack = None

    async def _open(self):
        if self._stack is None:
            raise WorkspaceError("AUTH_LIFETIME_REQUIRED")
        if self.context is None:
            self.context = await self._stack.enter_async_context(
                operation_browser(account_browser(self.profile, headed=self.headed))
            )
        return self.context

    @asynccontextmanager
    async def page(self):
        context = await self._open()
        page = await context.new_page()
        try:
            yield page
        finally:
            try:
                await page.close()
            except Exception:
                raise ContextNotReusable("Game page cleanup did not complete") from None

    async def ensure_session(self, provider, *, allow_login: bool = True):
        """Probe only the requested service; reauthenticate only when required."""
        context = await self._open()
        result = await coordinate_login(
            repo_root=MODULE_ROOT,
            context=context,
            page=provider.page,
            request_gate=self.gate,
            providers=(provider,),
            headed=self.headed and allow_login,
            poll_interval=2.0,
            timeout=self.timeout,
        )
        if result.interaction_required:
            self.version += 1
            # Existing owned-role proofs pin the account during this invocation.
            # Adapters use read-only Binding; no old game pages/headers are reused.
            for target, roles in self._owned.values():
                await target.initialize_session()
                observed = await target.owned_roles(context)
                if observed != roles:
                    raise WrongAccount("Shared login changed an already verified identity")
        return result

    async def owned_roles(self, provider) -> tuple[OwnedRole, ...]:
        from .discovery import DiscoveryProvider

        target = DiscoveryProvider(provider)
        result = await self.ensure_session(target)
        if not result.success or target.roles is None:
            raise WorkspaceError("LOGIN_BINDING_FAILED")
        previous = self._owned.get(provider.game)
        if previous is not None and previous[1] != target.roles:
            raise WrongAccount("Shared login changed an already verified identity")
        self._owned[provider.game] = (provider, target.roles)
        return target.roles

    def logout(self):
        if self._stack is None or self.context is not None:
            raise WorkspaceError("AUTH_LIFETIME_REQUIRED")
        if self.profile.exists():
            if profile_in_use(self.profile):
                raise WorkspaceError("BROWSER_PROFILE_IN_USE_OR_STALE")
            remove_owned_tree(self.profile)
        # Throttling is preserved. No old per-game authentication directories are read.
