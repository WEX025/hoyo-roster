"""Fixed ZZZ login Provider for the shared browser coordinator."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hoyo_roster.browser import initialize_session_page, BrowserContextAccess
from hoyo_roster.login import ExpectedIdentity, identity_digest
from hoyo_roster.login_ui import LoginPageConfig
from hoyo_roster.models import LoginProbeResult, LoginProbeStatus
from hoyo_roster.request_gate import RequestGate
from playwright.async_api import Page

from .browser import MIYOUSHE_ZZZ_URL, prepare_login_or_session
from .endpoints import EndpointClient, select_role
from .errors import (
    ApiResponseError,
    AuthenticationError,
    ConfigurationError,
    RateLimitedError,
    RequestTransportError,
    RiskControlError,
)
from .ids import parse_role_uid
from .transport import BrowserTransport

GAME = "zzz"
GAME_BIZ = "nap_cn"
EXPECTED_REGION = "prod_gf_cn"


LOGIN_UI = LoginPageConfig(
    "www.miyoushe.com", "/zzz/", "绝区零米游社登录页（www.miyoushe.com/zzz/）", panel_text="登录"
)


@dataclass(slots=True, repr=False)
class ZzzLoginProvider:
    context: BrowserContextAccess = field(repr=False)
    page: Page = field(repr=False)
    request_gate: RequestGate | None = field(repr=False)
    request_interval: float
    headed: bool = True
    endpoints: EndpointClient | None = field(default=None, repr=False)
    expected_uid_override: str | None = field(default=None, repr=False)
    game: str = field(default=GAME, init=False)
    login_ui: LoginPageConfig = field(default=LOGIN_UI, init=False)
    binding_data: dict[str, Any] | None = field(default=None, init=False, repr=False)
    _expected_uid: str | None = field(default=None, init=False, repr=False)
    _expected_region: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.endpoints is None:
            self.endpoints = EndpointClient(
                BrowserTransport(
                    self.context,
                    request_interval=self.request_interval,
                    request_gate=self.request_gate,
                )
            )

    def __repr__(self) -> str:
        return "ZzzLoginProvider(game='zzz')"

    async def expected_identity(self, repo_root: Path) -> ExpectedIdentity:
        if self.expected_uid_override is not None:
            uid = parse_role_uid(
                self.expected_uid_override,
                error_type=ConfigurationError,
            )
            self._expected_uid = uid
            self._expected_region = EXPECTED_REGION
            return ExpectedIdentity(
                game=self.game,
                digest=identity_digest(
                    game=self.game,
                    game_biz=GAME_BIZ,
                    uid=uid,
                    region=EXPECTED_REGION,
                ),
            )
        return ExpectedIdentity(game=self.game, digest=None)

    async def initialize_session(self) -> None:
        if self.request_gate is not None:
            await self.request_gate.reserve(requested_min_interval=self.request_interval)
        await initialize_session_page(self.context, MIYOUSHE_ZZZ_URL)

    async def probe(self, context: BrowserContextAccess) -> LoginProbeResult:
        if context is not self.context:
            raise ConfigurationError("ZZZ login probe context changed")
        if self._expected_uid is None or self._expected_region is None:
            raise ConfigurationError("expected ZZZ identity was not loaded")
        assert self.endpoints is not None
        try:
            binding_data = dict(await self.endpoints.get_binding())
        except AuthenticationError:
            return LoginProbeResult(LoginProbeStatus.AUTH_REQUIRED)
        except RateLimitedError:
            return LoginProbeResult(LoginProbeStatus.RATE_LIMITED)
        except RiskControlError:
            return LoginProbeResult(LoginProbeStatus.RISK_CONTROL)
        except RequestTransportError:
            return LoginProbeResult(LoginProbeStatus.NETWORK_ERROR)
        except ApiResponseError:
            return LoginProbeResult(LoginProbeStatus.BINDING_FAILED)
        try:
            role = select_role(binding_data, self._expected_uid)
        except ConfigurationError:
            return LoginProbeResult(LoginProbeStatus.WRONG_ACCOUNT)
        if role.server != self._expected_region:
            return LoginProbeResult(LoginProbeStatus.WRONG_ACCOUNT)
        self.binding_data = binding_data
        return LoginProbeResult(
            LoginProbeStatus.PASS,
            identity=identity_digest(
                game=self.game,
                game_biz=GAME_BIZ,
                uid=role.uid,
                region=role.server,
            ),
        )

    async def owned_roles(self, context):
        from hoyo_roster.models import OwnedRole
        from hoyo_roster.errors import (
            AuthenticationRequired,
            RateLimited,
            RiskControl,
            LoginNetworkError,
            LoginBindingFailed,
        )

        if context is not self.context:
            raise ConfigurationError("Owned-role probe context changed")
        try:
            data = dict(await self.endpoints.get_binding())
        except AuthenticationError:
            raise AuthenticationRequired("Official authentication required") from None
        except RateLimitedError:
            raise RateLimited("Official Binding rate limited") from None
        except RiskControlError:
            raise RiskControl("Official Binding risk control") from None
        except RequestTransportError:
            raise LoginNetworkError("Official Binding transport failed") from None
        except ApiResponseError:
            raise LoginBindingFailed("Official Binding invalid") from None
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise LoginBindingFailed("Official Binding list invalid")
        roles, seen = [], set()
        for item in data["list"]:
            if not isinstance(item, dict):
                raise LoginBindingFailed("Official Binding role invalid")
            if item.get("game_biz") != GAME_BIZ:
                continue
            try:
                uid = parse_role_uid(item.get("game_uid"))
                selected = select_role(data, uid)
                region = selected.server
                if region != EXPECTED_REGION:
                    raise ValueError
                if uid in seen:
                    raise ValueError
            except Exception:
                raise LoginBindingFailed("Official Binding identity invalid") from None
            seen.add(uid)
            roles.append(OwnedRole(self.game, uid, region))
        return tuple(sorted(roles, key=lambda role: (role.uid, role.region)))

    def login_page(self, fallback: Page) -> Page:
        return fallback

    async def prepare_login_or_session(
        self,
        context: BrowserContextAccess,
        page: Page,
        request_gate: RequestGate | None,
    ) -> None:
        if context is not self.context or page is not self.page:
            raise ConfigurationError("ZZZ login preparation context changed")
        if request_gate is not self.request_gate:
            raise ConfigurationError("ZZZ login request gate changed")
        await prepare_login_or_session(
            page=self.page,
            headed=self.headed,
            request_gate=self.request_gate,
            requested_min_interval=self.request_interval,
        )

    async def finish_login_or_session(
        self,
        context: BrowserContextAccess,
        page: Page,
    ) -> None:
        if context is not self.context or page is not self.page:
            raise ConfigurationError("ZZZ login finish context changed")

    def on_login_success(self) -> None:
        return


def create_routine_login_provider(
    *,
    context: BrowserContextAccess,
    page: Page,
    endpoints: EndpointClient,
    uid: str | None,
    headed: bool,
    request_gate: RequestGate | None,
    request_interval: float,
) -> ZzzLoginProvider:
    return ZzzLoginProvider(
        context=context,
        page=page,
        request_gate=request_gate,
        request_interval=request_interval,
        headed=headed,
        endpoints=endpoints,
        expected_uid_override=uid,
    )
