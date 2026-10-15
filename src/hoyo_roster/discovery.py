"""Provider-neutral owned-role discovery; never opens storage or calls the CLI."""

from __future__ import annotations

import hashlib
from typing import Protocol

from .models import OwnedRole
from .errors import LoginBindingFailed
from .login import ExpectedIdentity, LoginProvider
from .models import LoginProbeResult, LoginProbeStatus


class OwnedRoleTarget(LoginProvider, Protocol):
    page: object

    async def owned_roles(self, context) -> tuple[OwnedRole, ...]: ...


class DiscoveryProvider:
    def __init__(self, target: OwnedRoleTarget):
        self.target = target
        self.game = target.game
        self.page = target.page
        self.login_ui = target.login_ui
        self.roles = None
        self.selector = hashlib.sha256(f"owned-role-discovery:v2:{self.game}".encode()).hexdigest()

    async def expected_identity(self, repo_root):
        return ExpectedIdentity(self.game, self.selector)

    async def initialize_session(self):
        await self.target.initialize_session()

    async def probe(self, context):
        from .errors import AuthenticationRequired, RateLimited, RiskControl, LoginNetworkError

        try:
            self.roles = await self.target.owned_roles(context)
        except AuthenticationRequired:
            return LoginProbeResult(LoginProbeStatus.AUTH_REQUIRED)
        except RateLimited:
            return LoginProbeResult(LoginProbeStatus.RATE_LIMITED)
        except RiskControl:
            return LoginProbeResult(LoginProbeStatus.RISK_CONTROL)
        except LoginNetworkError:
            return LoginProbeResult(LoginProbeStatus.NETWORK_ERROR)
        except LoginBindingFailed:
            return LoginProbeResult(LoginProbeStatus.BINDING_FAILED)
        return LoginProbeResult(LoginProbeStatus.PASS, self.selector)

    def login_page(self, fallback):
        return self.target.login_page(fallback)

    async def prepare_login_or_session(self, context, page, request_gate):
        await self.target.prepare_login_or_session(context, page, request_gate)

    async def finish_login_or_session(self, context, page):
        await self.target.finish_login_or_session(context, page)

    def on_login_success(self):
        self.target.on_login_success()
