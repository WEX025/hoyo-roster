"""Persistent official-browser collection without credential export or request replay."""

from __future__ import annotations

import asyncio
import math
import os
from ... import platform_io as io
import stat
import time
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
    Response,
)

from .endpoint_specs import BINDING, CULTIVATION_LIST
from .errors import (
    ApiResponseError,
    AuthenticationError,
    ConfigurationError,
    LoginRequired,
    OfficialRequestNotObserved,
    RateLimitedError,
    RequestTransportError,
    RiskControlError,
    UnsupportedMultiPageContract,
)

CALCULATOR_URL = "https://act.mihoyo.com/ys/event/calculator/index.html#/character"
RISK_RETCODES = {1034, 10035}
AUTH_RETCODES = {-100, -101, 10001}
_RISK_CONTROL_MARKERS = ("验证码", "设备验证", "风险验证", "captcha", "geetest")


def contains_risk_control_text(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in _RISK_CONTROL_MARKERS)


def _assert_diagnostic_profile_authority(candidate: Path) -> Path:
    try:
        return resolve_private_profile_path(candidate)
    except DiagnosticProfileOverlap as error:
        raise ConfigurationError(str(error)) from None


def resolve_browser_profile(repo_root: Path, value: Path) -> Path:
    root = io.filesystem_path(repo_root).resolve()
    profile = io.filesystem_path(value)
    _assert_diagnostic_profile_authority(profile)
    if os.path.lexists(profile):
        metadata = profile.lstat()
        if io.is_redirect(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise ConfigurationError("browser profile must be a real directory")
    resolved = profile.resolve(strict=False)
    if resolved == root or resolved.is_relative_to(root):
        raise ConfigurationError("browser profile must be outside repo-root")
    try:
        from ...storage.common import secure_private_directory

        secure_private_directory(profile)
        metadata = profile.lstat()
    except OSError as error:
        raise ConfigurationError("browser profile could not be secured") from error
    if io.is_redirect(metadata) or not io.private(profile, 0o700):
        raise ConfigurationError("browser profile mode must be 0700")
    resolved_profile = profile.resolve()
    _assert_diagnostic_profile_authority(resolved_profile)
    return resolved_profile


@dataclass(slots=True)
class BrowserSession:
    playwright: Playwright | None
    context: BrowserContext
    page: Page
    headed: bool


async def open_login_page(
    session: BrowserSession,
    *,
    collector: OfficialCollector,
) -> None:
    await prepare_login_or_session(
        page=session.page,
        headed=session.headed,
        collector=collector,
    )


async def prepare_login_or_session(
    *,
    page: Page,
    headed: bool,
    collector: OfficialCollector,
) -> None:
    if not headed:
        raise LoginRequired("headless mode cannot perform the first login")
    await collector.reserve_navigation()
    await navigate_login_page(page, CALCULATOR_URL)


def _response_data(payload: object, *, endpoint: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ApiResponseError(f"{endpoint}: response root was invalid")
    retcode = payload.get("retcode")
    if isinstance(retcode, bool) or not isinstance(retcode, int):
        raise ApiResponseError(f"{endpoint}: retcode was invalid")
    if retcode in RISK_RETCODES:
        raise RiskControlError(f"{endpoint}: risk-control response")
    if retcode in AUTH_RETCODES:
        raise AuthenticationError(f"{endpoint}: login is invalid")
    if retcode != 0:
        raise ApiResponseError(f"{endpoint}: API rejected request")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ApiResponseError(f"{endpoint}: response data was invalid")
    return data


class OfficialCollector:
    def __init__(
        self,
        context: BrowserContext,
        page: Page,
        *,
        request_interval: float,
        request_gate: RequestGate | None = None,
    ) -> None:
        if not math.isfinite(request_interval) or request_interval < 1.0:
            raise ConfigurationError("request interval must be at least 1 second")
        self._context = context
        self._page = page
        self._interval = request_interval
        self._request_gate = request_gate
        self._last_request: float | None = None
        self.request_count = 0

    async def _throttle(self) -> None:
        if self._last_request is not None:
            remaining = self._interval - (time.monotonic() - self._last_request)
            if remaining > 0:
                await asyncio.sleep(remaining)

    async def _reserve(self) -> None:
        if self._request_gate is not None:
            await self._request_gate.reserve(requested_min_interval=self._interval)
            return
        await self._throttle()

    async def reserve_navigation(self) -> None:
        """Reserve one slot before a navigation that causes official requests."""

        await self._reserve()
        self._last_request = time.monotonic()

    async def binding(self) -> dict[str, Any]:
        await self._reserve()
        try:
            response = await self._context.request.get(
                BINDING.url,
                params={"game_biz": "hk4e_cn"},
                max_redirects=0,
                timeout=30_000,
            )
        except Exception:
            raise RequestTransportError("binding: request transport failed") from None
        finally:
            self._last_request = time.monotonic()
            self.request_count += 1
        if response.status == 429:
            raise RateLimitedError("binding: HTTP 429")
        if 300 <= response.status < 400:
            raise ApiResponseError("binding: redirect rejected")
        if response.status < 200 or response.status >= 300:
            raise ApiResponseError("binding: HTTP failure")
        try:
            data = _response_data(
                await asyncio.wait_for(response.json(), timeout=30), endpoint="binding"
            )
        except (AuthenticationError, RiskControlError, ApiResponseError):
            raise
        except Exception:
            raise ApiResponseError("binding: response was not JSON") from None
        if "list" not in data:
            raise ApiResponseError("binding: expected response fields were missing")
        return data

    @staticmethod
    def validate_list_request(request: Request, *, uid: str, region: str) -> dict[str, Any]:
        parsed = urlparse(request.url)
        if (
            request.method != CULTIVATION_LIST.method
            or parsed.scheme != CULTIVATION_LIST.scheme
            or parsed.hostname != CULTIVATION_LIST.host
            or parsed.path != CULTIVATION_LIST.path
        ):
            raise ApiResponseError("cultivation-avatar-list: endpoint mismatch")
        try:
            body = request.post_data_json
        except Exception:
            raise ApiResponseError("cultivation-avatar-list: body was invalid") from None
        required = {"element_attr_ids", "weapon_cat_ids", "page", "size", "uid", "region", "lang"}
        if not isinstance(body, Mapping) or set(body) != required:
            raise ApiResponseError("cultivation-avatar-list: body fields were invalid")
        if (
            type(body["element_attr_ids"]) is not list
            or body["element_attr_ids"] != []
            or type(body["weapon_cat_ids"]) is not list
            or body["weapon_cat_ids"] != []
        ):
            raise ApiResponseError("cultivation-avatar-list: filters were not empty")
        if type(body["page"]) is not int or body["page"] != 1:
            raise ApiResponseError("cultivation-avatar-list: paging request was invalid")
        if type(body["size"]) is not int or body["size"] < 1:
            raise ApiResponseError("cultivation-avatar-list: paging request was invalid")
        if (
            type(body["uid"]) is not str
            or body["uid"] != uid
            or type(body["region"]) is not str
            or body["region"] != region
        ):
            raise ApiResponseError("cultivation-avatar-list: identity or size was invalid")
        if type(body["lang"]) is not str or not body["lang"].strip():
            raise ApiResponseError("cultivation-avatar-list: language was invalid")
        return dict(body)

    async def cultivation_list(self, *, uid: str, region: str) -> tuple[dict[str, Any], int]:
        await self._reserve()
        await self._page.goto("about:blank", wait_until="domcontentloaded", timeout=30_000)
        loop = asyncio.get_running_loop()
        observed: asyncio.Future[Response] = loop.create_future()
        second_page = False

        def observe_request(request: Request) -> None:
            nonlocal second_page
            parsed = urlparse(request.url)
            if parsed.hostname != CULTIVATION_LIST.host or parsed.path != CULTIVATION_LIST.path:
                return
            try:
                body = request.post_data_json
            except Exception:
                body = None
            if isinstance(body, Mapping) and body.get("page") != 1:
                second_page = True

        def observe(response: Response) -> None:
            parsed = urlparse(response.url)
            if parsed.hostname != CULTIVATION_LIST.host or parsed.path != CULTIVATION_LIST.path:
                return
            if not observed.done():
                observed.set_result(response)

        self._page.on("request", observe_request)
        self._page.on("response", observe)
        try:
            await self._page.goto(CALCULATOR_URL, wait_until="domcontentloaded", timeout=30_000)
            try:
                response = await asyncio.wait_for(observed, timeout=45)
            except TimeoutError:
                try:
                    page_text = await self._page.locator("body").inner_text(timeout=1_000)
                except Exception:
                    page_text = ""
                if contains_risk_control_text(page_text):
                    raise RiskControlError(
                        "official page requires manual risk-control confirmation"
                    ) from None
                raise OfficialRequestNotObserved(
                    "official cultivation character request was not observed"
                ) from None
            body = self.validate_list_request(response.request, uid=uid, region=region)
            if response.status == 429:
                raise RateLimitedError("cultivation-avatar-list: HTTP 429")
            if 300 <= response.status < 400:
                raise ApiResponseError("cultivation-avatar-list: redirect rejected")
            if response.status < 200 or response.status >= 300:
                raise ApiResponseError("cultivation-avatar-list: HTTP failure")
            try:
                data = _response_data(
                    await asyncio.wait_for(response.json(), timeout=30),
                    endpoint="cultivation-avatar-list",
                )
            except (AuthenticationError, RiskControlError, ApiResponseError):
                raise
            except Exception:
                raise ApiResponseError("cultivation-avatar-list: response was not JSON") from None
            await self._page.wait_for_timeout(1_000)
            if second_page:
                raise UnsupportedMultiPageContract("official page requested another page")
            return data, int(body["size"])
        finally:
            self._page.remove_listener("request", observe_request)
            self._page.remove_listener("response", observe)
            self._last_request = time.monotonic()
            self.request_count += 1


@asynccontextmanager
async def shared_browser(auth):
    """Borrow one game page; the auth service retains ownership of the context."""
    async with auth.page() as page:
        session = BrowserSession(None, auth.context, page, auth.headed)
        yield session
