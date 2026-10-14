"""Fixed Genshin login Provider for the shared browser coordinator."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hoyo_roster.browser import initialize_session_page, BrowserContextAccess
from hoyo_roster.login import ExpectedIdentity, identity_digest
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
from hoyo_roster.jsonio import load_json
from .normalize import nonempty_string, positive_id, select_binding

GAME = "genshin"
GAME_BIZ = "hk4e_cn"
PROFILE_PATH = Path("data/genshin-impact/profile.json")
SUPPORTED_REGIONS = frozenset({"cn_gf01", "cn_qd01"})


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
        payload = load_json(repo_root / PROFILE_PATH)
        if not isinstance(payload, dict) or payload.get("game") != "genshin-impact":
            raise ConfigurationError("formal Genshin Profile contract was invalid")
        account = payload.get("account")
        if not isinstance(account, dict):
            raise ConfigurationError("formal Genshin account was invalid")
        raw_uid = account.get("uid")
        raw_region = account.get("server")
        if raw_uid is None and raw_region is None:
            if self.expected_uid_override is None:
                return ExpectedIdentity(game=self.game, digest=None)
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
        if raw_uid is None or raw_region is None:
            raise ConfigurationError("formal Genshin identity was incomplete")
        uid = positive_id(raw_uid, label="formal Genshin UID")
        region = nonempty_string(raw_region, label="formal Genshin region")
        if region not in SUPPORTED_REGIONS:
            raise ConfigurationError("formal Genshin region was unsupported")
        if self.expected_region_override is not None and region != self.expected_region_override:
            raise ConfigurationError("discovered Genshin region did not match formal Profile")
        if self.expected_uid_override is not None and uid != self.expected_uid_override:
            raise ConfigurationError("requested Genshin UID did not match formal Profile")
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
