"""Fixed Genshin login Provider for the shared browser coordinator."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hoyo_roster.browser import initialize_session_page, BrowserContextAccess
from hoyo_roster.login import ExpectedIdentity, identity_digest
from hoyo_roster.login_ui import LoginPageConfig
from hoyo_roster.models import LoginProbeResult, LoginProbeStatus
from hoyo_roster.request_gate import RequestGate
from playwright.async_api import Page

from .browser import CALCULATOR_URL, OfficialCollector, prepare_login_or_session
from .errors import (
    ApiResponseError,
    AuthenticationError,
    ConfigurationError,
    ExpectedUidAmbiguous,
    ExpectedUidNotFound,
    RateLimitedError,
    RequestTransportError,
    RiskControlError,
)
from .normalize import positive_id, select_binding

GAME = "genshin"
GAME_BIZ = "hk4e_cn"
SUPPORTED_REGIONS = frozenset({"cn_gf01", "cn_qd01"})


LOGIN_UI = LoginPageConfig(
    "act.mihoyo.com",
    "/ys/event/calculator/",
    "原神养成计算器登录页（act.mihoyo.com/ys/event/calculator/）",
    panel_selector=".tw-fixed.tw-top-0 img.tw-cursor-pointer.tw-rounded-full",
)


@dataclass(slots=True, repr=False)
class GenshinLoginProvider:
    context: BrowserContextAccess = field(repr=False)
    page: Page = field(repr=False)
    request_gate: RequestGate | None = field(repr=False)
    request_interval: float
    headed: bool = True
    collector: OfficialCollector | None = field(default=None, repr=False)
    expected_uid_override: str | None = field(default=None, repr=False)
    expected_region_override: str | None = field(default=None, repr=False)
    game: str = field(default=GAME, init=False)
    login_ui: LoginPageConfig = field(default=LOGIN_UI, init=False)
    binding_data: dict[str, Any] | None = field(default=None, init=False, repr=False)
    _expected_uid: str | None = field(default=None, init=False, repr=False)
    _expected_region: str | None = field(default=None, init=False, repr=False)
    _bootstrap_digest: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.collector is None:
            self.collector = OfficialCollector(
                self.context,
                self.page,
                request_interval=self.request_interval,
                request_gate=self.request_gate,
            )

    def __repr__(self) -> str:
        return "GenshinLoginProvider(game='genshin')"

    async def expected_identity(self, repo_root: Path) -> ExpectedIdentity:
        self._expected_uid = None
        self._expected_region = None
        self._bootstrap_digest = None
        self.binding_data = None
        if self.expected_uid_override is not None:
            uid = positive_id(self.expected_uid_override, label="requested Genshin UID")
            self._expected_uid = uid
            if self.expected_region_override is not None:
                if self.expected_region_override not in SUPPORTED_REGIONS:
                    raise ConfigurationError("requested Genshin region was unsupported")
                self._expected_region = self.expected_region_override
            # First use knows the requested UID, not its official region. This digest is
            # a domain-separated selector; probe still proves exact game/UID and a
            # supported region from Binding before it can report PASS.
            self._bootstrap_digest = hashlib.sha256(
                f"genshin-login-bootstrap:v1:{GAME_BIZ}:{uid}".encode()
            ).hexdigest()
            return ExpectedIdentity(game=self.game, digest=self._bootstrap_digest)
        return ExpectedIdentity(game=self.game, digest=None)

    async def initialize_session(self) -> None:
        await self.collector.reserve_navigation()
        await initialize_session_page(self.context, CALCULATOR_URL)

    async def probe(self, context: BrowserContextAccess) -> LoginProbeResult:
        if context is not self.context:
            raise ConfigurationError("Genshin login probe context changed")
        if self._expected_uid is None or (
            self._expected_region is None and self._bootstrap_digest is None
        ):
            raise ConfigurationError("expected Genshin identity was not loaded")
        assert self.collector is not None
        try:
            binding_data = await self.collector.binding()
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
            binding = select_binding(binding_data, self._expected_uid)
        except (ExpectedUidNotFound, ExpectedUidAmbiguous):
            return LoginProbeResult(LoginProbeStatus.WRONG_ACCOUNT)
        if binding["server"] not in SUPPORTED_REGIONS:
            return LoginProbeResult(LoginProbeStatus.BINDING_FAILED)
        if self._expected_region is not None and binding["server"] != self._expected_region:
            return LoginProbeResult(LoginProbeStatus.WRONG_ACCOUNT)
        self._expected_region = binding["server"]
        self.binding_data = binding_data
        return LoginProbeResult(
            LoginProbeStatus.PASS,
            identity=self._bootstrap_digest
            or identity_digest(
                game=self.game,
                game_biz=GAME_BIZ,
                uid=binding["uid"],
                region=binding["server"],
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
            data = await self.collector.binding()
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
                uid = positive_id(item.get("game_uid"))
                selected = select_binding(data, uid)
                region = selected["server"]
                if region not in SUPPORTED_REGIONS:
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
            raise ConfigurationError("Genshin login preparation context changed")
        if request_gate is not self.request_gate:
            raise ConfigurationError("Genshin login request gate changed")
        assert self.collector is not None
        await prepare_login_or_session(
            page=self.page,
            headed=self.headed,
            collector=self.collector,
        )

    async def finish_login_or_session(
        self,
        context: BrowserContextAccess,
        page: Page,
    ) -> None:
        if context is not self.context or page is not self.page:
            raise ConfigurationError("Genshin login finish context changed")

    def on_login_success(self) -> None:
        return


def create_routine_login_provider(
    *,
    context: BrowserContextAccess,
    page: Page,
    collector: OfficialCollector,
    uid: str,
    headed: bool,
    request_gate: RequestGate | None,
    request_interval: float,
    expected_region: str | None = None,
) -> GenshinLoginProvider:
    return GenshinLoginProvider(
        context=context,
        page=page,
        request_gate=request_gate,
        request_interval=request_interval,
        headed=headed,
        collector=collector,
        expected_uid_override=uid,
        expected_region_override=expected_region,
    )
