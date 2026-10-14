"""Official page initialization without exporting credentials or recording."""

import asyncio
from urllib.parse import urlparse
from playwright.async_api import BrowserContext, Page
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from .errors import (
    ContextNotReusable,
    LoginControlUnavailable,
    LoginNetworkError,
    LoginTimeout,
    LoginSessionInitializationFailed,
    RateLimited,
    RiskControl,
)
from .progress import OPTIONS


def chromium_profile_path(profile) -> str:
    # Keep extended paths for protected filesystem I/O, but Chromium's Windows
    # cookie persistence requires an ordinary drive-letter user-data-dir.
    value = str(profile)
    if value.startswith("\\\\?\\") and len(profile.drive) == 6 and profile.drive[5] == ":":
        return value[4:]
    return value


type BrowserContextAccess = BrowserContext


def login_timeout() -> float:
    options = OPTIONS.get()
    return min(30.0, options.timeout) if options else 30.0


async def navigate_login_page(page: Page, url: str) -> None:
    try:
        async with asyncio.timeout(login_timeout()):
            response = await page.goto(
                url, wait_until="domcontentloaded", timeout=login_timeout() * 1000
            )
    except (TimeoutError, PlaywrightTimeoutError):
        raise LoginTimeout("Official login page did not finish loading") from None
    except PlaywrightError as error:
        if page.is_closed():
            raise LoginControlUnavailable(
                "Official login page closed; reopen the command"
            ) from None
        if "net::ERR_" in str(error):
            raise LoginNetworkError("Official login page unavailable") from None
        raise LoginControlUnavailable("Official login page control unavailable") from None

    check_navigation_response(response)
    # Foreground control has its own bounded phase after navigation completes.
    try:
        async with asyncio.timeout(login_timeout()):
            await page.bring_to_front()
    except (TimeoutError, PlaywrightError):
        raise LoginControlUnavailable("Official login page control unavailable") from None


def check_navigation_response(response) -> None:
    if response is None:
        return
    if response.status == 429:
        raise RateLimited("Official page was rate limited")
    if response.status >= 400:
        raise LoginNetworkError("Official page returned an HTTP failure")


async def check_session_validation(response) -> None:
    check_navigation_response(response)
    try:
        retcode = (await response.json()).get("retcode")
    except Exception:
        raise LoginSessionInitializationFailed("Official session validation was invalid") from None
    if type(retcode) is not int:
        raise LoginSessionInitializationFailed("Official session validation retcode was invalid")
    if retcode in {1034, 10035, 5003, 10041}:
        raise RiskControl("Official session validation reported risk control")
    if retcode not in {0, -100, -101, 10001}:
        raise LoginSessionInitializationFailed("Official session validation failed")
    # Even a successful refresh is only initialization, never proof of identity.
    # An expired refresh is resolved by the following exact Binding, not retried.


async def guard_session_initialization(route) -> None:
    """Allow page resources and existing-session validation, never login or game APIs."""
    request = route.request
    parsed = urlparse(request.url)
    host = parsed.hostname or ""
    official = any(
        host == suffix or host.endswith("." + suffix)
        for suffix in ("mihoyo.com", "miyoushe.com", "hoyoverse.com", "hoyolab.com")
    )
    safe_origin = (
        parsed.scheme == "https"
        and parsed.port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and official
    )
    api_host = host.startswith(("api.", "api-")) or "-api." in host or "-api-" in host
    resource = (
        not api_host
        and request.method == "GET"
        and request.resource_type
        in {
            "document",
            "script",
            "stylesheet",
            "image",
            "font",
            "media",
            "manifest",
        }
    )
    session_validation = (
        host == "passport-api.mihoyo.com"
        and request.method == "POST"
        and parsed.path == "/account/ma-cn-session/web/verifyCookieToken"
    )
    if safe_origin and (resource or session_validation):
        await route.continue_()
    else:
        await route.abort("blockedbyclient")


async def initialize_session_page(context: BrowserContextAccess, url: str) -> None:
    """Initialize the official SDK's existing session before the first Binding.

    The temporary guarded page shares the persistent context but cannot send SMS,
    create a QR login or collect game data. Readiness is bounded and cancellable.
    """
    page = None
    primary_error = None
    validation_tasks = []

    def observe(response) -> None:
        parsed = urlparse(response.url)
        if (
            parsed.hostname == "passport-api.mihoyo.com"
            and parsed.path == "/account/ma-cn-session/web/verifyCookieToken"
        ):
            validation_tasks.append(asyncio.create_task(check_session_validation(response)))

    try:
        async with asyncio.timeout(login_timeout()):
            page = await context.new_page()
            await page.route("**/*", guard_session_initialization)
            page.on("response", observe)
            response = await page.goto(
                url, wait_until="networkidle", timeout=login_timeout() * 1000
            )
            check_navigation_response(response)
            if validation_tasks:
                outcomes = await asyncio.gather(*validation_tasks, return_exceptions=True)
                # Protection outranks all other outcomes; no Binding follows a failure.
                for kind in (RateLimited, RiskControl, BaseException):
                    for outcome in outcomes:
                        if isinstance(outcome, kind):
                            raise outcome
    except (TimeoutError, PlaywrightTimeoutError):
        primary_error = LoginTimeout("Official session initialization timed out")
        raise primary_error from None
    except PlaywrightError as error:
        primary_error = (
            LoginNetworkError("Official session initialization transport failed")
            if "net::ERR_" in str(error)
            else LoginControlUnavailable("Official session initialization control unavailable")
        )
        raise primary_error from None
    except BaseException as error:
        primary_error = error
        raise
    finally:
        for task in validation_tasks:
            if not task.done():
                task.cancel()
        if validation_tasks:
            await asyncio.gather(*validation_tasks, return_exceptions=True)
        if page is not None:
            page.remove_listener("response", observe)
            try:
                # Keep the guard installed until close; queued game requests stay blocked.
                await page.close()
            except PlaywrightError:
                if primary_error is None:
                    raise ContextNotReusable("Session initialization cleanup failed") from None
