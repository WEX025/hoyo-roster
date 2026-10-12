"""Fixed ZZZ login Provider for the shared browser coordinator."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hoyo_browser_session.browser import BrowserContextAccess
from hoyo_browser_session.login import ExpectedIdentity, identity_digest
from hoyo_browser_session.models import LoginProbeResult, LoginProbeStatus
from hoyo_browser_session.request_gate import RequestGate
from playwright.async_api import Page

from .browser import prepare_login_or_session
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
from .jsonio import load_json
from .transport import BrowserTransport

GAME = "zzz"
GAME_BIZ = "nap_cn"
PROFILE_PATH = Path("data/zenless-zone-zero/profile.json")
EXPECTED_REGION = "prod_gf_cn"


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
        payload = load_json(repo_root / PROFILE_PATH)
        if not isinstance(payload, dict) or payload.get("game") != "zenless-zone-zero":
            raise ConfigurationError("formal ZZZ Profile contract was invalid")
        account = payload.get("account")
        if not isinstance(account, dict):
            raise ConfigurationError("formal ZZZ account was invalid")
        raw_uid = account.get("uid")
        raw_region = account.get("server")
        if raw_uid is None and raw_region is None:
            return ExpectedIdentity(game=self.game, digest=None)
        if raw_uid is None or raw_region is None:
            raise ConfigurationError("formal ZZZ identity was incomplete")
        uid = parse_role_uid(raw_uid, error_type=ConfigurationError)
        if not isinstance(raw_region, str) or not raw_region.strip():
            raise ConfigurationError("formal ZZZ region was invalid")
        region = raw_region
        self._expected_uid = uid
        self._expected_region = region
        return ExpectedIdentity(
            game=self.game,
            digest=identity_digest(
                game=self.game,
                game_biz=GAME_BIZ,
                uid=uid,
                region=region,
            ),
        )

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


def create_login_provider(
    *,
    context: BrowserContextAccess,
    page: Page,
    request_gate: RequestGate,
    request_interval: float,
) -> ZzzLoginProvider:
    return ZzzLoginProvider(
        context=context,
        page=page,
        request_gate=request_gate,
        request_interval=request_interval,
    )


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
