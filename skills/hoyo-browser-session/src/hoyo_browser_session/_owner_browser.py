"""Private persistent-browser authority used only by the Browser Owner."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from playwright.async_api import BrowserContext, Page, Playwright, Route, async_playwright
from playwright.async_api import Error as PlaywrightError

from .browser import seal_context_pages
from .config import configure_playwright_environment
from .errors import BrokerError, ContextNotReusable, RuntimeNotReady
from .profile import ProfileManager
from .redaction import diagnostic_hash

CHROMIUM_INSTALL_COMMAND = "python -m playwright install chromium"
OWNER_BROWSER_LAUNCH_TIMEOUT_MS = 60_000


@dataclass(slots=True)
class OwnerBrowserSession:
    playwright: Playwright = field(repr=False)
    context: BrowserContext = field(repr=False)
    page: Page = field(repr=False)
    headed: bool
    network_blocked: bool


def _cleanup_error(
    error: BaseException,
    *,
    context: str,
) -> ContextNotReusable:
    diagnostic = diagnostic_hash(error, context=context)
    return ContextNotReusable(
        f"Browser cleanup failed; diagnostic_sha256={diagnostic[:12]}"
    )


@asynccontextmanager
async def persistent_owner_browser(
    profile_manager: ProfileManager,
    *,
    repo_root: Path,
    headed: bool,
    block_network: bool = False,
) -> AsyncIterator[OwnerBrowserSession]:
    """Open and close the one fixed primary Profile under Owner authority."""

    profile_lock = profile_manager._acquire_owner_lock(repo_root=repo_root)
    playwright: Playwright | None = None
    context: BrowserContext | None = None
    primary_error: BaseException | None = None
    try:
        configure_playwright_environment(profile_manager.settings)
        try:
            playwright = await async_playwright().start()
        except PlaywrightError as error:
            diagnostic = diagnostic_hash(error, context="playwright-start")
            raise ContextNotReusable(
                f"Playwright start failed; diagnostic_sha256={diagnostic[:12]}"
            ) from None
        executable = Path(playwright.chromium.executable_path)
        if not executable.is_file():
            raise RuntimeNotReady("Playwright Chromium is not installed")
        try:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_manager.user_data_dir),
                headless=not headed,
                timeout=OWNER_BROWSER_LAUNCH_TIMEOUT_MS,
                accept_downloads=False,
                args=[
                    "--disable-background-networking",
                    "--disable-breakpad",
                    "--disable-component-update",
                    "--disable-domain-reliability",
                    "--disable-sync",
                    "--metrics-recording-only",
                    "--no-first-run",
                ],
            )
        except PlaywrightError as error:
            diagnostic = diagnostic_hash(error, context="chromium-launch")
            raise ContextNotReusable(
                f"Chromium launch failed; diagnostic_sha256={diagnostic[:12]}"
            ) from None
        if block_network:

            async def block(route: Route) -> None:
                await route.abort("blockedbyclient")

            await context.route("**/*", block)
        page = context.pages[0] if context.pages else await context.new_page()
        await seal_context_pages(context, page)
        yield OwnerBrowserSession(
            playwright=playwright,
            context=context,
            page=page,
            headed=headed,
            network_blocked=block_network,
        )
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_error: BaseException | None = None
        if context is not None:
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await seal_context_pages(context, page)
                await context.close()
            except BaseException as error:
                cleanup_error = _cleanup_error(error, context="browser-context-close")
        if playwright is not None:
            try:
                await playwright.stop()
            except BaseException as error:
                if cleanup_error is None:
                    cleanup_error = _cleanup_error(error, context="playwright-stop")
        try:
            profile_manager.postflight()
        except BaseException as error:
            if cleanup_error is None:
                cleanup_error = (
                    error
                    if isinstance(error, BrokerError)
                    else _cleanup_error(error, context="profile-postflight")
                )
        try:
            profile_lock.release()
        except BaseException as error:
            if cleanup_error is None:
                cleanup_error = _cleanup_error(error, context="profile-lock-release")
        if primary_error is None and cleanup_error is not None:
            raise cleanup_error
