"""Immutable registry of the only authenticated endpoints this Skill may call."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from .errors import ConfigurationError
from .pagination import PaginationContract

HttpMethod = Literal["GET", "POST"]


@dataclass(frozen=True, slots=True)
class EndpointSpec:
    name: str
    scheme: Literal["https"]
    host: str
    path: str
    method: HttpMethod
    purpose: str
    allows_body: bool
    allows_device_id: bool
    required_response_fields: tuple[str, ...]
    allows_auth_cookie: bool
    allows_redirect: bool
    pagination_contract: PaginationContract | None

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.host}{self.path}"


BINDING = EndpointSpec(
    name="binding",
    scheme="https",
    host="api-takumi.mihoyo.com",
    path="/binding/api/getUserGameRolesByCookie",
    method="GET",
    purpose="select the logged-in ZZZ role",
    allows_body=False,
    allows_device_id=True,
    required_response_fields=("list",),
    allows_auth_cookie=True,
    allows_redirect=False,
    pagination_contract=None,
)
AVATAR_BASIC = EndpointSpec(
    name="record:avatar/basic",
    scheme="https",
    host="api-takumi-record.mihoyo.com",
    path="/event/game_record_zzz/api/zzz/avatar/basic",
    method="GET",
    purpose="read the complete owned Agent roster",
    allows_body=False,
    allows_device_id=True,
    required_response_fields=("avatar_list",),
    allows_auth_cookie=True,
    allows_redirect=False,
    pagination_contract=None,
)
AVATAR_INFO = EndpointSpec(
    name="record:avatar/info",
    scheme="https",
    host="api-takumi-record.mihoyo.com",
    path="/event/game_record_zzz/api/zzz/avatar/info",
    method="GET",
    purpose="read one owned Agent's current build",
    allows_body=False,
    allows_device_id=True,
    required_response_fields=("avatar_list",),
    allows_auth_cookie=True,
    allows_redirect=False,
    pagination_contract=None,
)
BUDDY_INFO = EndpointSpec(
    name="record:buddy/info",
    scheme="https",
    host="api-takumi-record.mihoyo.com",
    path="/event/game_record_zzz/api/zzz/buddy/info",
    method="GET",
    purpose="read the complete owned Bangboo pool",
    allows_body=False,
    allows_device_id=True,
    required_response_fields=("list",),
    allows_auth_cookie=True,
    allows_redirect=False,
    pagination_contract=None,
)
CULTIVATE_AVATAR_BASIC = EndpointSpec(
    name="cultivate:user/avatar_basic_list",
    scheme="https",
    host="api-takumi.mihoyo.com",
    path="/event/nap_cultivate_tool/user/avatar_basic_list",
    method="GET",
    purpose="cross-check the complete Agent ownership set",
    allows_body=False,
    allows_device_id=False,
    required_response_fields=("list",),
    allows_auth_cookie=True,
    allows_redirect=False,
    pagination_contract=None,
)
CULTIVATE_BATCH_DETAIL = EndpointSpec(
    name="cultivate:user/batch_avatar_detail_v2",
    scheme="https",
    host="api-takumi.mihoyo.com",
    path="/event/nap_cultivate_tool/user/batch_avatar_detail_v2",
    method="POST",
    purpose="read permanent progression for owned Agents",
    allows_body=True,
    allows_device_id=False,
    required_response_fields=("list",),
    allows_auth_cookie=True,
    allows_redirect=False,
    pagination_contract=None,
)

_REGISTERED_ENDPOINTS = (
    BINDING,
    AVATAR_BASIC,
    AVATAR_INFO,
    BUDDY_INFO,
    CULTIVATE_AVATAR_BASIC,
    CULTIVATE_BATCH_DETAIL,
)
ENDPOINT_REGISTRY = MappingProxyType(
    {endpoint.name: endpoint for endpoint in _REGISTERED_ENDPOINTS}
)


def require_registered_endpoint(endpoint: object) -> EndpointSpec:
    """Validate registered source metadata before requesting a client read."""

    if not isinstance(endpoint, EndpointSpec):
        raise ConfigurationError("endpoint must be a registered immutable EndpointSpec")
    registered = ENDPOINT_REGISTRY.get(endpoint.name)
    if registered is not endpoint:
        raise ConfigurationError("endpoint is not the registered immutable EndpointSpec")
    if (
        endpoint.scheme != "https"
        or not endpoint.host
        or not endpoint.path.startswith("/")
        or endpoint.method not in {"GET", "POST"}
        or endpoint.allows_redirect
        or not endpoint.allows_auth_cookie
    ):
        raise ConfigurationError("registered EndpointSpec violates the network boundary")
    if endpoint.method == "GET" and endpoint.allows_body:
        raise ConfigurationError("GET EndpointSpec cannot allow a body")
    if endpoint.method == "POST" and not endpoint.allows_body:
        raise ConfigurationError("POST EndpointSpec must declare its body")
    return endpoint
