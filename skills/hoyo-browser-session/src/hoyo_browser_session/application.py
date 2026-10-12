"""Composition root for the fixed four-package Hoyo application.

Broker imports remain usable without game packages. Business operations resolve
this code-owned composition only when an adapter or login provider is needed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import cache
from types import MappingProxyType
from typing import cast

from playwright.async_api import Page

from .browser import BrowserContextAccess
from .login import LoginProvider
from .operation import OperationAdapter, OperationKey
from .request_gate import RequestGate

LoginProviderFactory = Callable[
    [BrowserContextAccess, Page, RequestGate, float, str | None],
    LoginProvider,
]


@cache
def owner_operation_registry() -> Mapping[OperationKey, OperationAdapter]:
    from genshin_character_state_sync.operation_adapter import GENSHIN_PUBLISH_ADAPTER
    from hsr_character_state_sync.operation_adapter import (
        HSR_BOOTSTRAP_ADAPTER,
        HSR_DRY_RUN_ADAPTER,
        HSR_PUBLISH_ADAPTER,
    )
    from zzz_character_state_sync.operation_adapter import ZZZ_PUBLISH_ADAPTER

    from .login_operation import LOGIN_PRIMARY_ADAPTER
    from .operation_registry import OWNER_OPERATIONS

    adapters = (
        GENSHIN_PUBLISH_ADAPTER,
        ZZZ_PUBLISH_ADAPTER,
        HSR_PUBLISH_ADAPTER,
        HSR_BOOTSTRAP_ADAPTER,
        HSR_DRY_RUN_ADAPTER,
        LOGIN_PRIMARY_ADAPTER,
    )
    registry = {adapter.identity.key: adapter for adapter in adapters}
    if len(registry) != len(adapters) or set(registry) != OWNER_OPERATIONS:
        raise RuntimeError("fixed Owner operation registry identity was inconsistent")
    if any(
        type(adapter.current_contract_version) is not int or adapter.current_contract_version < 1
        for adapter in adapters
    ):
        raise RuntimeError("fixed Owner operation contract version was invalid")
    return MappingProxyType(registry)


def fixed_genshin_factory(
    context: BrowserContextAccess,
    page: Page,
    request_gate: RequestGate,
    request_interval: float,
    bootstrap_identity: str | None,
) -> LoginProvider:
    del bootstrap_identity
    from genshin_character_state_sync.login_provider import (
        create_login_provider,
    )

    return cast(
        LoginProvider,
        create_login_provider(
            context=context,
            page=page,
            request_gate=request_gate,
            request_interval=request_interval,
        ),
    )


def fixed_zzz_factory(
    context: BrowserContextAccess,
    page: Page,
    request_gate: RequestGate,
    request_interval: float,
    bootstrap_identity: str | None,
) -> LoginProvider:
    del bootstrap_identity
    from zzz_character_state_sync.login_provider import (
        create_login_provider,
    )

    return cast(
        LoginProvider,
        create_login_provider(
            context=context,
            page=page,
            request_gate=request_gate,
            request_interval=request_interval,
        ),
    )


def fixed_hsr_factory(
    context: BrowserContextAccess,
    page: Page,
    request_gate: RequestGate,
    request_interval: float,
    bootstrap_identity: str | None,
) -> LoginProvider:
    from hsr_character_state_sync.login_provider import (
        create_login_provider,
    )

    return cast(
        LoginProvider,
        create_login_provider(
            context=context,
            page=page,
            request_gate=request_gate,
            request_interval=request_interval,
            bootstrap_uid=bootstrap_identity,
        ),
    )


LOGIN_PROVIDER_FACTORIES: Mapping[str, LoginProviderFactory] = MappingProxyType(
    {
        "genshin": fixed_genshin_factory,
        "zzz": fixed_zzz_factory,
        "hsr": fixed_hsr_factory,
    }
)
