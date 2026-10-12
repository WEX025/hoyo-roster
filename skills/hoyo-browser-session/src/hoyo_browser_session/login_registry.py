"""Fixed login-provider facade for the four-package application."""

from __future__ import annotations

from playwright.async_api import Page

from .application import (
    LOGIN_PROVIDER_FACTORIES as LOGIN_PROVIDER_FACTORIES,
)
from .application import (
    LoginProviderFactory as LoginProviderFactory,
)
from .application import (
    fixed_genshin_factory as fixed_genshin_factory,
)
from .application import (
    fixed_hsr_factory as fixed_hsr_factory,
)
from .application import (
    fixed_zzz_factory as fixed_zzz_factory,
)
from .browser import BrowserContextAccess
from .login import LoginProvider
from .request_gate import RequestGate


def create_fixed_login_providers(
    *,
    context: BrowserContextAccess,
    page: Page,
    request_gate: RequestGate,
    request_interval: float,
    bootstrap_identity: str | None,
) -> tuple[LoginProvider, ...]:
    """Construct the fixed three Providers through one uniform factory contract."""

    return tuple(
        factory(
            context,
            page,
            request_gate,
            request_interval,
            bootstrap_identity,
        )
        for factory in LOGIN_PROVIDER_FACTORIES.values()
    )
