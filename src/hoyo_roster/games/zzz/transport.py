"""Rate-limited requests sharing the browser context cookie jar."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import secrets
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from hoyo_roster.request_gate import RequestGate
from playwright.async_api import BrowserContext

from .endpoint_specs import EndpointSpec, require_registered_endpoint
from .errors import (
    ApiResponseError,
    AuthenticationError,
    ConfigurationError,
    RateLimitedError,
    RequestTransportError,
    RiskControlError,
)

CN_DS_SALT = "xV8v4Qu54lUKrEYFZkJhB8cuOh9Asafs"
RISK_RETCODES = {10035, 5003, 10041, 1034}
AUTH_RETCODES = {-100, -101, 10001}
_CULTIVATE_COOKIES = ("e_nap_token", "e_lrsag", "SERVERID", "SERVERCORSID")


def _json_body(body: Mapping[str, Any] | None) -> str:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")) if body else ""


def generate_cn_ds(
    *,
    body: Mapping[str, Any] | None = None,
    params: Mapping[str, object] | None = None,
    timestamp: int | None = None,
    nonce: int | None = None,
) -> str:
    """Generate the CN DS value entirely in memory."""

    current = int(time.time()) if timestamp is None else timestamp
    random_value = 100001 + secrets.randbelow(100000) if nonce is None else nonce
    query = "&".join(f"{key}={value}" for key, value in sorted((params or {}).items()))
    # MD5 is mandated by the official DS protocol; it is not used for password hashing.
    digest = hashlib.md5(
        f"salt={CN_DS_SALT}&t={current}&r={random_value}&b={_json_body(body)}&q={query}".encode()
    ).hexdigest()
    return f"{current},{random_value},{digest}"


class BrowserTransport:
    """Validate every HTTP and API layer without logging request material."""

    def __init__(
        self,
        context: BrowserContext,
        *,
        request_interval: float,
        rpc_headers: Mapping[str, str] | None = None,
        request_gate: RequestGate | None = None,
    ) -> None:
        if not math.isfinite(request_interval) or request_interval < 1.0:
            raise ConfigurationError("request interval must be at least 1 second")
        self._context = context
        self._interval = request_interval
        self._rpc_headers = rpc_headers if rpc_headers is not None else {}
        self._request_gate = request_gate
        self._last_request_at: float | None = None
        self._request_lock = asyncio.Lock()
        self.request_count = 0

    async def has_cookie(self, name: str) -> bool:
        cookies = await self._context.cookies()
        return any(cookie.get("name") == name and cookie.get("value") for cookie in cookies)

    async def clear_cultivate_cookies(self) -> None:
        for name in _CULTIVATE_COOKIES:
            await self._context.clear_cookies(name=name)

    async def _wait_for_slot(self) -> None:
        if self._request_gate is not None:
            await self._request_gate.reserve(requested_min_interval=self._interval)
            return
        if self._last_request_at is None:
            return
        delay = self._interval - (time.monotonic() - self._last_request_at)
        if delay > 0:
            await asyncio.sleep(delay)

    def _headers(
        self,
        *,
        body: Mapping[str, Any] | None,
        params: Mapping[str, object] | None,
        include_device_id: bool,
    ) -> dict[str, str]:
        headers = {
            "DS": generate_cn_ds(body=body, params=params),
            "Origin": "https://www.miyoushe.com",
            "Referer": "https://www.miyoushe.com/",
            "x-rpc-app_version": "2.11.1",
            "x-rpc-client_type": "5",
            "x-rpc-lang": "zh-cn",
            "x-rpc-language": "zh-cn",
        }
        for key, value in self._rpc_headers.items():
            lowered = key.lower()
            if lowered == "x-rpc-device_id" and not include_device_id:
                continue
            if lowered.startswith("x-rpc-"):
                headers[lowered] = value
        if body is not None:
            headers["Content-Type"] = "application/json"
        return headers

    async def request_json(
        self,
        endpoint: EndpointSpec,
        *,
        params: Mapping[str, object] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return validated response data and discard the response object."""

        spec = require_registered_endpoint(endpoint)
        if body is not None and not spec.allows_body:
            raise ConfigurationError("EndpointSpec does not allow a request body")
        if spec.method == "POST" and body is None:
            raise ConfigurationError("POST EndpointSpec requires a request body")
        request_params: dict[str, str | float | bool] | None = (
            {key: str(value) for key, value in params.items()} if params is not None else None
        )
        headers = self._headers(
            body=body,
            params=request_params,
            include_device_id=spec.allows_device_id,
        )
        async with self._request_lock:
            await self._wait_for_slot()
            try:
                if spec.method == "GET":
                    response = await self._context.request.get(
                        spec.url,
                        params=request_params,
                        headers=headers,
                        max_redirects=0,
                        timeout=30_000,
                    )
                else:
                    response = await self._context.request.post(
                        spec.url,
                        params=request_params,
                        data=_json_body(body),
                        headers=headers,
                        max_redirects=0,
                        timeout=30_000,
                    )
            except Exception:
                raise RequestTransportError(f"{spec.name}: request transport failed") from None
            finally:
                self._last_request_at = time.monotonic()
                self.request_count += 1

        if response.status == 429:
            raise RateLimitedError(f"{spec.name}: HTTP 429")
        if 300 <= response.status < 400:
            location = response.headers.get("location", "")
            redirect_host = urlparse(location).hostname if location else None
            if redirect_host and redirect_host != spec.host:
                raise ApiResponseError(f"{spec.name}: cross-host redirect rejected")
            raise ApiResponseError(f"{spec.name}: redirect rejected")
        if response.status < 200 or response.status >= 300:
            raise ApiResponseError(f"{spec.name}: HTTP status {response.status}")
        try:
            payload: Any = await asyncio.wait_for(response.json(), timeout=30)
        except Exception:
            raise ApiResponseError(f"{spec.name}: response was not JSON") from None
        if not isinstance(payload, dict):
            raise ApiResponseError(f"{spec.name}: response root was not an object")
        retcode = payload.get("retcode")
        message = payload.get("message")
        if type(retcode) is not int or not isinstance(message, str):
            raise ApiResponseError(f"{spec.name}: retcode or message was missing")
        if retcode in RISK_RETCODES:
            raise RiskControlError(f"{spec.name}: risk-control retcode {retcode}")
        if retcode in AUTH_RETCODES:
            raise AuthenticationError(f"{spec.name}: login is invalid")
        if retcode != 0:
            raise ApiResponseError(f"{spec.name}: API retcode {retcode}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ApiResponseError(f"{spec.name}: data was not an object")
        missing = [field for field in spec.required_response_fields if field not in data]
        if missing:
            raise ApiResponseError(f"{spec.name}: expected response fields were missing")
        return data
