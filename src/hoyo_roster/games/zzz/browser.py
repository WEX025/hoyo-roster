"""Persistent browser session handling without credential export."""

from __future__ import annotations

import asyncio
import os
from ... import platform_io as io
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from hoyo_roster.browser import navigate_login_page
from hoyo_roster.config import (
    resolve_private_profile_path,
)
from hoyo_roster.errors import DiagnosticProfileOverlap
from hoyo_roster.request_gate import RequestGate
from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    Request,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)

from .errors import ConfigurationError, LoginRequired, RiskControlError

MIYOUSHE_ZZZ_URL = "https://www.miyoushe.com/zzz/"
CHROMIUM_INSTALL_COMMAND = "python -m playwright install chromium"
_CAPTURED_RPC_HEADERS = (
    "x-rpc-device_id",
    "x-rpc-device_fp",
    "x-rpc-geetest_ext",
    "x-rpc-page",
    "x-rpc-platform",
)
_RISK_CONTROL_MARKERS = ("验证码", "设备验证", "风险验证", "captcha", "geetest")


def _assert_diagnostic_profile_authority(candidate: Path) -> Path:
    try:
        return resolve_private_profile_path(candidate)
    except DiagnosticProfileOverlap as error:
        raise ConfigurationError(str(error)) from None


def resolve_browser_profile(repo_root: Path, browser_profile: Path) -> Path:
    """Create a private real directory outside the repository."""

    root = repo_root.expanduser().resolve()
    profile = browser_profile.expanduser().absolute()
    _assert_diagnostic_profile_authority(profile)
    if os.path.lexists(profile):
        try:
            metadata = profile.lstat()
        except OSError as error:
            raise ConfigurationError("browser profile inspection failed") from error
        if io.is_redirect(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise ConfigurationError("browser profile must be a real directory")
    resolved_profile = profile.resolve(strict=False)
    if resolved_profile == root or resolved_profile.is_relative_to(root):
        raise ConfigurationError("browser profile must be outside repo-root")
    try:
        from ...storage.common import secure_private_directory

        secure_private_directory(profile)
        metadata = profile.lstat()
    except OSError as error:
        raise ConfigurationError("browser profile could not be secured") from error
    if (
        io.is_redirect(metadata)
        or not stat.S_ISDIR(metadata.st_mode)
        or not io.private(profile, 0o700)
    ):
        raise ConfigurationError("browser profile mode must be 0700")
    resolved_profile = profile.resolve()
    _assert_diagnostic_profile_authority(resolved_profile)
    return resolved_profile


@dataclass
class BrowserSession:
    """Browser objects plus device headers captured only in process memory."""

    playwright: Playwright | None
    context: BrowserContext
    page: Page
    headed: bool
    rpc_headers: dict[str, str] = field(default_factory=dict)


def _capture_rpc_headers(session: BrowserSession, request: Request) -> None:
    headers = {key.lower(): value for key, value in request.headers.items()}
    for key in _CAPTURED_RPC_HEADERS:
        value = headers.get(key)
        if value:
            session.rpc_headers[key] = value


@asynccontextmanager
async def persistent_browser(
    *,
    repo_root: Path,
    browser_profile: Path,
    headed: bool,
) -> AsyncIterator[BrowserSession]:
    """Open a persistent context with all recording features disabled."""

    profile = resolve_browser_profile(repo_root, browser_profile)
    playwright = await async_playwright().start()
    context: BrowserContext | None = None
    try:
        if not Path(playwright.chromium.executable_path).is_file():
            raise ConfigurationError(
                f"Playwright Chromium is not installed; run: {CHROMIUM_INSTALL_COMMAND}"
            )
        try:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                headless=not headed,
                accept_downloads=False,
            )
        except PlaywrightError as exc:
            message = str(exc).lower()
            if "executable doesn't exist" in message or "executable does not exist" in message:
                raise ConfigurationError(
                    f"Playwright Chromium is not installed; run: {CHROMIUM_INSTALL_COMMAND}"
                ) from None
            raise ConfigurationError("Playwright Chromium failed to start") from None
        page = context.pages[0] if context.pages else await context.new_page()
        session = BrowserSession(
            playwright=playwright,
            context=context,
            page=page,
            headed=headed,
        )
        page.on("request", lambda request: _capture_rpc_headers(session, request))
        yield session
    finally:
        try:
            if context is not None:
                await context.close()
        finally:
            await playwright.stop()


async def open_login_page(
    session: BrowserSession,
    *,
    request_gate: RequestGate | None,
    requested_min_interval: float,
) -> None:
    await prepare_login_or_session(
        page=session.page,
        headed=session.headed,
        request_gate=request_gate,
        requested_min_interval=requested_min_interval,
    )


async def prepare_login_or_session(
    *,
    page: Page,
    headed: bool,
    request_gate: RequestGate | None,
    requested_min_interval: float,
) -> None:
    if not headed:
        raise LoginRequired("headless mode cannot perform the first login")
    if request_gate is not None:
        await request_gate.reserve(requested_min_interval=requested_min_interval)
    await navigate_login_page(page, MIYOUSHE_ZZZ_URL)


async def prime_rpc_headers(
    session: BrowserSession,
    *,
    timeout_ms: int = 10_000,
    request_gate: RequestGate | None = None,
    requested_min_interval: float = 1.8,
) -> None:
    """Capture official device headers in memory before API requests."""

    required = {"x-rpc-device_id", "x-rpc-device_fp"}
    if request_gate is not None:
        await request_gate.reserve(requested_min_interval=requested_min_interval)
    await session.page.goto(MIYOUSHE_ZZZ_URL, wait_until="domcontentloaded")
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while not required <= session.rpc_headers.keys():
        if asyncio.get_running_loop().time() >= deadline:
            try:
                page_text = await session.page.locator("body").inner_text(timeout=1_000)
            except Exception:
                page_text = ""
            lowered = page_text.lower()
            if any(marker in lowered for marker in _RISK_CONTROL_MARKERS):
                raise RiskControlError("official page requires manual risk-control confirmation")
            raise ConfigurationError("official page did not provide required device headers")
        await session.page.wait_for_timeout(100)
    await session.page.goto("about:blank", wait_until="domcontentloaded")
