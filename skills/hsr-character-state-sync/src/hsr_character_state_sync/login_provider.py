"""Fixed HSR login Provider with a page-scoped login guard."""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hoyo_browser_session.browser import BrowserContextAccess, navigate_login_page
from hoyo_browser_session.errors import (
    AuthenticationRequired as BrokerAuthenticationRequired,
)
from hoyo_browser_session.errors import (
    BootstrapIdentityAlreadyEstablished,
    ExpectedIdentityConflict,
)
from hoyo_browser_session.login import ExpectedIdentity, identity_digest
from hoyo_browser_session.login_identity import LoginIdentityStore
from hoyo_browser_session.models import LoginProbeResult, LoginProbeStatus
from hoyo_browser_session.request_gate import RequestGate
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Route

from .browser import (
    CULTIVATION_URL,
    BrowserSession,
    OfficialCollector,
    guard_login_request,
)
from .errors import (
    ApiResponseError,
    AuthenticationError,
    ConfigurationError,
    ExpectedUidNotFound,
    RateLimitedError,
    RequestTransportError,
    RiskControlError,
)
from .jsonio import load_json
from .normalize import nonempty_string, positive_id, select_binding

GAME = "hsr"
GAME_BIZ = "hkrpg_cn"
EXPECTED_REGION = "prod_gf_cn"
PROFILE_PATH = Path("data/honkai-star-rail/profile.json")
RouteHandler = Callable[[Route], Awaitable[None]]


@dataclass(slots=True, repr=False)
class HsrLoginProvider:
    context: BrowserContextAccess = field(repr=False)
    page: Page = field(repr=False)
    request_gate: RequestGate | None = field(repr=False)
    request_interval: float
    headed: bool = True
    collector: OfficialCollector | None = field(default=None, repr=False)
    expected_uid_override: str | None = field(default=None, repr=False)
    bootstrap_uid: str | None = field(default=None, repr=False)
    identity_store: LoginIdentityStore | None = field(default=None, repr=False)
    session: BrowserSession | None = field(default=None, repr=False)
    game: str = field(default=GAME, init=False)
    binding: dict[str, Any] | None = field(default=None, init=False, repr=False)
    expected_identity_source: str = field(default="UNAVAILABLE", init=False)
    _expected_uid: str | None = field(default=None, init=False, repr=False)
    _expected_region: str | None = field(default=None, init=False, repr=False)
    _expected_fingerprint: str | None = field(default=None, init=False, repr=False)
    _expected_raw_digest: str | None = field(default=None, init=False, repr=False)
    _record_allowed: bool = field(default=False, init=False, repr=False)
    _login_page: Page | None = field(default=None, init=False, repr=False)
    _route_handler: RouteHandler | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.collector is None:
            self.collector = OfficialCollector(
                self.context,
                request_interval=self.request_interval,
                request_gate=self.request_gate,
            )
        if self.identity_store is None and self.request_gate is not None:
            self.identity_store = LoginIdentityStore(self.request_gate.settings)

    def __repr__(self) -> str:
        return "HsrLoginProvider(game='hsr')"

    async def expected_identity(self, repo_root: Path) -> ExpectedIdentity:
        payload = load_json(repo_root / PROFILE_PATH)
        if not isinstance(payload, dict) or payload.get("game") != "honkai-star-rail":
            raise ConfigurationError("formal HSR Profile contract was invalid")
        account = payload.get("account")
        if not isinstance(account, dict):
            raise ConfigurationError("formal HSR account was invalid")
        raw_uid = account.get("uid")
        raw_region = account.get("server")
        formal_identity: tuple[str, str] | None
        if raw_uid is None and raw_region is None:
            formal_identity = None
        elif raw_uid is None or raw_region is None:
            raise ConfigurationError("formal HSR identity was incomplete")
        else:
            uid = positive_id(raw_uid, label="formal HSR UID")
            region = nonempty_string(raw_region, label="formal HSR region")
            if region != EXPECTED_REGION:
                raise ConfigurationError("formal HSR region was unsupported")
            formal_identity = (uid, region)

        local_identity = (
            self.identity_store.read(self.game) if self.identity_store is not None else None
        )
        if self.bootstrap_uid is not None:
            if formal_identity is not None or local_identity is not None:
                raise BootstrapIdentityAlreadyEstablished(
                    "HSR bootstrap identity is already established"
                )
            uid = positive_id(self.bootstrap_uid, label="bootstrap HSR UID")
            self.expected_identity_source = "BOOTSTRAP_UID_IN_MEMORY"
            self._record_allowed = True
            return self._set_plaintext_expected(uid, EXPECTED_REGION)

        if self.expected_uid_override is not None:
            uid = positive_id(
                self.expected_uid_override,
                label="expected HSR UID",
            )
            raw_digest = self._raw_digest(uid, EXPECTED_REGION)
            if formal_identity is not None and formal_identity != (
                uid,
                EXPECTED_REGION,
            ):
                raise ExpectedIdentityConflict(
                    "formal HSR identity conflicts with routine identity"
                )
            if (
                local_identity is not None
                and self.identity_store is not None
                and not hmac.compare_digest(
                    local_identity.fingerprint,
                    self.identity_store.fingerprint(self.game, raw_digest),
                )
            ):
                raise ExpectedIdentityConflict(
                    "verified HSR identity conflicts with routine identity"
                )
            self.expected_identity_source = "ROUTINE_UID_IN_MEMORY"
            self._record_allowed = formal_identity is None and local_identity is None
            return self._set_plaintext_expected(uid, EXPECTED_REGION)

        if formal_identity is not None:
            uid, region = formal_identity
            raw_digest = self._raw_digest(uid, region)
            if (
                local_identity is not None
                and self.identity_store is not None
                and not hmac.compare_digest(
                    local_identity.fingerprint,
                    self.identity_store.fingerprint(self.game, raw_digest),
                )
            ):
                raise ExpectedIdentityConflict("formal and verified HSR identities conflict")
            self.expected_identity_source = "FORMAL_PROFILE"
            return self._set_plaintext_expected(uid, region)

        if local_identity is not None:
            self.expected_identity_source = "LOCAL_VERIFIED_HMAC"
            self._expected_fingerprint = local_identity.fingerprint
            return ExpectedIdentity(
                game=self.game,
                digest=local_identity.fingerprint,
            )

        self.expected_identity_source = "UNAVAILABLE"
        return ExpectedIdentity(game=self.game, digest=None)

    @staticmethod
    def _raw_digest(uid: str, region: str) -> str:
        return identity_digest(
            game=GAME,
            game_biz=GAME_BIZ,
            uid=uid,
            region=region,
        )

    def _set_plaintext_expected(
        self,
        uid: str,
        region: str,
    ) -> ExpectedIdentity:
        self._expected_uid = uid
        self._expected_region = region
        self._expected_raw_digest = self._raw_digest(uid, region)
        return ExpectedIdentity(
            game=self.game,
            digest=self._expected_raw_digest,
        )

    async def probe(self, context: BrowserContextAccess) -> LoginProbeResult:
        if context is not self.context:
            raise ConfigurationError("HSR login probe context changed")
        if self._expected_uid is None and self._expected_fingerprint is None:
            raise ConfigurationError("expected HSR identity was not loaded")
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
        if self._expected_uid is not None:
            try:
                binding = select_binding(binding_data, self._expected_uid)
            except ExpectedUidNotFound:
                return LoginProbeResult(LoginProbeStatus.WRONG_ACCOUNT)
            if binding["server"] != self._expected_region:
                return LoginProbeResult(LoginProbeStatus.WRONG_ACCOUNT)
            result_identity = self._raw_digest(
                binding["uid"],
                binding["server"],
            )
        else:
            fingerprint_binding = self._select_fingerprint_binding(binding_data)
            if fingerprint_binding is None:
                return LoginProbeResult(LoginProbeStatus.WRONG_ACCOUNT)
            binding = fingerprint_binding
            assert self.identity_store is not None
            raw_digest = self._raw_digest(binding["uid"], binding["server"])
            result_identity = self.identity_store.fingerprint(
                self.game,
                raw_digest,
            )
            self._expected_raw_digest = raw_digest
        self.binding = binding
        return LoginProbeResult(
            LoginProbeStatus.PASS,
            identity=result_identity,
        )

    def _select_fingerprint_binding(
        self,
        binding_data: dict[str, Any],
    ) -> dict[str, Any] | None:
        roles = binding_data.get("list")
        if not isinstance(roles, list):
            raise ConfigurationError("HSR Binding list was invalid")
        if self.identity_store is None or self._expected_fingerprint is None:
            raise ConfigurationError("verified HSR identity was unavailable")
        matches: list[str] = []
        seen: set[tuple[str, str]] = set()
        for raw in roles:
            if not isinstance(raw, dict):
                raise ConfigurationError("HSR Binding role was invalid")
            if raw.get("game_biz") != GAME_BIZ:
                continue
            if raw.get("region") != EXPECTED_REGION:
                continue
            uid = positive_id(raw.get("game_uid"), label="HSR Binding UID")
            key = (uid, EXPECTED_REGION)
            if key in seen:
                raise ConfigurationError("HSR Binding identity was duplicated")
            seen.add(key)
            raw_digest = self._raw_digest(uid, EXPECTED_REGION)
            fingerprint = self.identity_store.fingerprint(
                self.game,
                raw_digest,
            )
            if hmac.compare_digest(
                fingerprint,
                self._expected_fingerprint,
            ):
                matches.append(uid)
        if not matches:
            return None
        if len(matches) != 1:
            raise ConfigurationError("verified HSR identity matched multiple Bindings")
        return select_binding(binding_data, matches[0])

    def record_verified_identity(self) -> bool:
        if (
            not self._record_allowed
            or self.identity_store is None
            or self.binding is None
            or self._expected_raw_digest is None
        ):
            return False
        actual = self._raw_digest(
            self.binding["uid"],
            self.binding["server"],
        )
        if not hmac.compare_digest(actual, self._expected_raw_digest):
            raise ExpectedIdentityConflict("exact HSR Binding changed before identity recording")
        recorded = self.identity_store.record_verified(self.game, actual)
        self._record_allowed = False
        return recorded

    def on_login_success(self) -> None:
        self.record_verified_identity()

    async def prepare_login_or_session(
        self,
        context: BrowserContextAccess,
        page: Page,
        request_gate: RequestGate | None,
    ) -> None:
        if context is not self.context or page is not self.page:
            raise ConfigurationError("HSR login preparation context changed")
        if request_gate is not self.request_gate:
            raise ConfigurationError("HSR login request gate changed")
        if not self.headed:
            raise BrokerAuthenticationRequired("headed HSR login is required")
        if self.session is not None:
            self.session.begin_login()
        assert self.collector is not None
        try:
            await self.collector.reserve_navigation()
            login_page = await self.context.new_page()

            async def page_login_guard(route: Route) -> None:
                await guard_login_request(route, session=self.session)

            self._login_page = login_page
            self._route_handler = page_login_guard
            await login_page.route("**/*", page_login_guard)
            await navigate_login_page(login_page, CULTIVATION_URL)
        except (PlaywrightError, OSError):
            cleanup_page = self._login_page
            handler = self._route_handler
            if cleanup_page is not None:
                if handler is not None:
                    with suppress(PlaywrightError):
                        await cleanup_page.unroute("**/*", handler)
                with suppress(PlaywrightError):
                    await cleanup_page.close()
            self._login_page = None
            self._route_handler = None
            if self.session is not None:
                self.session.end_login()
            raise BrokerAuthenticationRequired("HSR login page could not be prepared") from None

    async def finish_login_or_session(
        self,
        context: BrowserContextAccess,
        page: Page,
    ) -> None:
        if context is not self.context or page is not self.page:
            raise ConfigurationError("HSR login finish context changed")
        login_page = self._login_page
        handler = self._route_handler
        try:
            if login_page is not None:
                await login_page.goto("about:blank", wait_until="commit")
                if handler is not None:
                    await login_page.unroute("**/*", handler)
                await login_page.close()
            if self.session is not None and self.session.login_page_open_count:
                self.session.login_page_sealed = True
        except PlaywrightError:
            raise BrokerAuthenticationRequired("HSR login page could not be sealed") from None
        finally:
            self._login_page = None
            self._route_handler = None
            if self.session is not None:
                self.session.end_login()


def create_login_provider(
    *,
    context: BrowserContextAccess,
    page: Page,
    request_gate: RequestGate,
    request_interval: float,
    bootstrap_uid: str | None = None,
) -> HsrLoginProvider:
    return HsrLoginProvider(
        context=context,
        page=page,
        request_gate=request_gate,
        request_interval=request_interval,
        bootstrap_uid=bootstrap_uid,
    )


def create_routine_login_provider(
    *,
    collector: OfficialCollector,
    session: BrowserSession,
    uid: str,
) -> HsrLoginProvider:
    return HsrLoginProvider(
        context=session.context,
        page=session.page,
        request_gate=session.request_gate,
        request_interval=session.requested_min_interval,
        headed=session.headed,
        collector=collector,
        expected_uid_override=uid,
        session=session,
    )
