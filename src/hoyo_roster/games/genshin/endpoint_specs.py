"""Immutable allowlist for the only authenticated business endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from .errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class EndpointSpec:
    name: str
    scheme: Literal["https"]
    host: str
    path: str
    method: Literal["GET", "POST"]
    allows_body: bool
    allows_auth_cookie: bool
    allows_redirect: bool
    purpose: str
    required_response_fields: tuple[str, ...]

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.host}{self.path}"


BINDING = EndpointSpec(
    name="binding",
    scheme="https",
    host="passport-api.mihoyo.com",
    path="/binding/api/getUserGameRolesByLtoken",
    method="GET",
    allows_body=False,
    allows_auth_cookie=True,
    allows_redirect=False,
    purpose="verify login and select one exact Genshin binding",
    required_response_fields=("list",),
)
CULTIVATION_LIST = EndpointSpec(
    name="cultivation-avatar-list",
    scheme="https",
    host="api-takumi.mihoyo.com",
    path="/event/e20200928calculate/v1/sync/avatar/list",
    method="POST",
    allows_body=True,
    allows_auth_cookie=True,
    allows_redirect=False,
    purpose="read the complete owned Genshin character roster",
    required_response_fields=("list", "total"),
)

_REGISTRY = MappingProxyType({item.name: item for item in (BINDING, CULTIVATION_LIST)})


def require_registered_endpoint(value: object) -> EndpointSpec:
    if not isinstance(value, EndpointSpec) or _REGISTRY.get(value.name) is not value:
        raise ConfigurationError("endpoint is not registered")
    if value.scheme != "https" or value.allows_redirect or not value.allows_auth_cookie:
        raise ConfigurationError("registered endpoint violates the network boundary")
    if (value.method == "GET") == value.allows_body:
        raise ConfigurationError("registered endpoint body contract is invalid")
    return value
