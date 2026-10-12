"""Provider-neutral login coordination driven only by read-only Binding probes."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import math
import string
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from playwright.async_api import Page

from .browser import BrowserContextAccess
from .errors import (
    AuthenticationRequired,
    ContextNotReusable,
    LoginBindingFailed,
    LoginNetworkError,
    LoginTimeout,
    RateLimited,
    RiskControl,
    WrongAccount,
)
from .login_ui import choose_method, confirm_method_entry
from .progress import OPTIONS, emit
from .models import LoginProbeResult, LoginProbeStatus
from .request_gate import RequestGate

LOGIN_GAMES = ("genshin", "zzz", "hsr")


@dataclass(frozen=True, slots=True)
class ExpectedIdentity:
    """A provider-owned exact identity represented to the shared layer by digest."""

    game: str
    digest: str | None = field(repr=False)

    def __post_init__(self) -> None:
        if self.game not in LOGIN_GAMES:
            raise ValueError("login identity game was not registered")
        if self.digest is not None and (
            len(self.digest) != 64
            or any(character not in string.hexdigits for character in self.digest)
        ):
            raise ValueError("login identity digest was invalid")

    @property
    def available(self) -> bool:
        return self.digest is not None


class LoginCoverageStatus(StrEnum):
    EXPECTED_IDENTITY_UNAVAILABLE = "EXPECTED_IDENTITY_UNAVAILABLE"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    WRONG_ACCOUNT = "WRONG_ACCOUNT"
    RATE_LIMITED = "RATE_LIMITED"
    RISK_CONTROL = "RISK_CONTROL"
    NETWORK_ERROR = "NETWORK_ERROR"
    BINDING_FAILED = "BINDING_FAILED"
    PASS = "PASS"


@dataclass(frozen=True, slots=True)
class LoginCoverage:
    game: str
    status: LoginCoverageStatus


@dataclass(frozen=True, slots=True)
class LoginCoordinatorResult:
    coverage: tuple[LoginCoverage, ...]
    interaction_required: bool
    waiting_signal_count: int

    @property
    def available_coverage_success(self) -> bool:
        return any(item.status is LoginCoverageStatus.PASS for item in self.coverage) and all(
            item.status
            in {
                LoginCoverageStatus.PASS,
                LoginCoverageStatus.EXPECTED_IDENTITY_UNAVAILABLE,
            }
            for item in self.coverage
        )

    @property
    def complete_coverage_success(self) -> bool:
        return bool(self.coverage) and all(
            item.status is LoginCoverageStatus.PASS for item in self.coverage
        )

    @property
    def success(self) -> bool:
        return self.complete_coverage_success


class LoginProvider(Protocol):
    game: str

    async def expected_identity(self, repo_root: Path) -> ExpectedIdentity: ...

    async def initialize_session(self) -> None: ...

    async def probe(self, context: BrowserContextAccess) -> LoginProbeResult: ...

    async def prepare_login_or_session(
        self,
        context: BrowserContextAccess,
        page: Page,
        request_gate: RequestGate | None,
    ) -> None: ...

    async def finish_login_or_session(
        self,
        context: BrowserContextAccess,
        page: Page,
    ) -> None: ...

    def on_login_success(self) -> None: ...


@dataclass(slots=True)
class CallbackLoginProvider:
    """Bind an existing game collector to the common login state machine."""

    game: str
    identity: ExpectedIdentity
    probe_callback: Callable[[], Awaitable[LoginProbeResult]] = field(repr=False)
    prepare_callback: Callable[[], Awaitable[None]] = field(repr=False)
    finish_callback: Callable[[], Awaitable[None]] = field(repr=False)
    success_callback: Callable[[], None] = field(default=lambda: None, repr=False)

    def __post_init__(self) -> None:
        if self.game != self.identity.game:
            raise ValueError("callback login provider identity game mismatch")

    async def expected_identity(self, repo_root: Path) -> ExpectedIdentity:
        del repo_root
        return self.identity

    async def initialize_session(self) -> None:
        return

    async def probe(self, context: BrowserContextAccess) -> LoginProbeResult:
        del context
        return await self.probe_callback()

    async def prepare_login_or_session(
        self,
        context: BrowserContextAccess,
        page: Page,
        request_gate: RequestGate | None,
    ) -> None:
        del context, page, request_gate
        await self.prepare_callback()

    async def finish_login_or_session(
        self,
        context: BrowserContextAccess,
        page: Page,
    ) -> None:
        del context, page
        await self.finish_callback()

    def on_login_success(self) -> None:
        self.success_callback()


def identity_digest(*, game: str, game_biz: str, uid: str, region: str) -> str:
    if game not in LOGIN_GAMES:
        raise ValueError("identity digest game was not registered")
    if not game_biz or not uid or not region:
        raise ValueError("identity digest inputs must be non-empty")
    return hashlib.sha256(f"{game}:{game_biz}:{region}:{uid}".encode()).hexdigest()


def _coverage_status(
    result: LoginProbeResult,
    expected: ExpectedIdentity,
) -> LoginCoverageStatus:
    if result.status is LoginProbeStatus.PASS:
        assert result.identity is not None
        if expected.digest is not None and hmac.compare_digest(
            result.identity,
            expected.digest,
        ):
            return LoginCoverageStatus.PASS
        return LoginCoverageStatus.WRONG_ACCOUNT
    return LoginCoverageStatus(result.status.value)


def _raise_terminal(coverage: Sequence[LoginCoverage]) -> None:
    if any(item.status is LoginCoverageStatus.RATE_LIMITED for item in coverage):
        raise RateLimited("login coverage probe was rate limited")
    if any(item.status is LoginCoverageStatus.RISK_CONTROL for item in coverage):
        raise RiskControl("login coverage probe reported risk control")
    if any(item.status is LoginCoverageStatus.NETWORK_ERROR for item in coverage):
        raise LoginNetworkError("login Binding transport failed")
    if any(item.status is LoginCoverageStatus.BINDING_FAILED for item in coverage):
        raise LoginBindingFailed("login Binding response was invalid")


async def _probe_coverage(
    *,
    context: BrowserContextAccess,
    providers: Sequence[LoginProvider],
    identities: Sequence[ExpectedIdentity],
) -> tuple[LoginCoverage, ...]:
    coverage: list[LoginCoverage] = []
    for provider, expected in zip(providers, identities, strict=True):
        if not expected.available:
            coverage.append(
                LoginCoverage(
                    game=provider.game,
                    status=LoginCoverageStatus.EXPECTED_IDENTITY_UNAVAILABLE,
                )
            )
            continue
        result = await provider.probe(context)
        coverage.append(
            LoginCoverage(
                game=provider.game,
                status=_coverage_status(result, expected),
            )
        )
        # Stop immediately on protection or service failure; do not send later probes.
        _raise_terminal(coverage)
    resolved = tuple(coverage)
    _raise_terminal(resolved)
    return resolved


def _notify_login_success(
    providers: Sequence[LoginProvider],
    coverage: Sequence[LoginCoverage],
) -> None:
    result = LoginCoordinatorResult(tuple(coverage), False, 0)
    if result.available_coverage_success:
        for provider, item in zip(providers, coverage, strict=True):
            if item.status is LoginCoverageStatus.PASS:
                provider.on_login_success()


async def coordinate_login(
    *,
    repo_root: Path,
    context: BrowserContextAccess,
    page: Page,
    request_gate: RequestGate | None,
    providers: Sequence[LoginProvider],
    headed: bool,
    poll_interval: float,
    timeout: float,
    waiting_callback: Callable[[], None] | None = None,
    initial_coverage_callback: Callable[[tuple[LoginCoverage, ...]], None] | None = None,
) -> LoginCoordinatorResult:
    """Require simultaneous exact coverage without recreating the browser context."""

    if not providers:
        raise ValueError("at least one login provider is required")
    games = tuple(provider.game for provider in providers)
    if len(set(games)) != len(games) or any(game not in LOGIN_GAMES for game in games):
        raise ValueError("login providers must have unique registered games")
    if (
        not math.isfinite(poll_interval)
        or poll_interval < 0
        or not math.isfinite(timeout)
        or timeout < 0
    ):
        raise ValueError("login timing values must be finite and non-negative")

    emit("CHECKING_SESSION")
    options = OPTIONS.get()
    if options is not None:
        timeout = options.timeout
    root = repo_root.expanduser().resolve(strict=True)
    identities = tuple([await provider.expected_identity(root) for provider in providers])
    if tuple(identity.game for identity in identities) != games:
        raise ValueError("login provider returned an identity for another game")

    prepared: list[LoginProvider] = []
    primary_error: BaseException | None = None
    result: LoginCoordinatorResult | None = None
    waiting_signal_count = 0
    interaction_required = False

    def notify_waiting() -> None:
        nonlocal waiting_signal_count, interaction_required
        interaction_required = True
        if waiting_callback is not None and not waiting_signal_count:
            waiting_callback()
            waiting_signal_count = 1

    async def prepare(provider: LoginProvider) -> None:
        # A provider may create a Page or route before navigation fails.
        method = await choose_method()
        prepared.append(provider)
        await provider.prepare_login_or_session(context, page, request_gate)
        login_page = getattr(provider, "_login_page", None) or page
        await confirm_method_entry(login_page, method)
        emit("VERIFYING_IDENTITY")

    try:
        for provider, identity in zip(providers, identities, strict=True):
            if identity.available:
                emit("INITIALIZING_SESSION")
                await provider.initialize_session()
        coverage = await _probe_coverage(
            context=context,
            providers=providers,
            identities=identities,
        )
        for item in coverage:
            print(f"SESSION_BINDING={item.status.value}", flush=True)
        if initial_coverage_callback is not None:
            initial_coverage_callback(coverage)
        initial = LoginCoordinatorResult(coverage, interaction_required, waiting_signal_count)
        if initial.available_coverage_success or not any(
            identity.available for identity in identities
        ):
            result = initial
        else:
            if any(item.status is LoginCoverageStatus.WRONG_ACCOUNT for item in coverage):
                raise WrongAccount("login coverage did not match the expected account")
            if not headed:
                raise AuthenticationRequired("headed login is required")

            for provider, item in zip(providers, coverage, strict=True):
                if provider in prepared or item.status in {
                    LoginCoverageStatus.PASS,
                    LoginCoverageStatus.EXPECTED_IDENTITY_UNAVAILABLE,
                }:
                    continue
                await prepare(provider)
                notify_waiting()

            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while True:
                coverage = await _probe_coverage(
                    context=context,
                    providers=providers,
                    identities=identities,
                )
                if any(item.status is LoginCoverageStatus.WRONG_ACCOUNT for item in coverage):
                    raise WrongAccount("login coverage did not match the expected account")
                observed = LoginCoordinatorResult(coverage, True, waiting_signal_count)
                if observed.available_coverage_success:
                    result = observed
                    break
                if loop.time() >= deadline:
                    raise LoginTimeout("login coverage probe timed out")
                await asyncio.sleep(min(poll_interval, max(0.0, deadline - loop.time())))
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_failed = False
        cleanup_cancelled: asyncio.CancelledError | None = None
        for provider in reversed(prepared):
            try:
                await provider.finish_login_or_session(context, page)
            except asyncio.CancelledError as error:
                cleanup_failed = True
                if cleanup_cancelled is None:
                    cleanup_cancelled = error
            except BaseException:
                cleanup_failed = True
        if cleanup_failed:
            if primary_error is None:
                if cleanup_cancelled is not None:
                    raise cleanup_cancelled
                raise ContextNotReusable("Login browser cleanup was incomplete")
    assert result is not None
    emit("VERIFYING_IDENTITY")
    _notify_login_success(providers, result.coverage)
    return result
