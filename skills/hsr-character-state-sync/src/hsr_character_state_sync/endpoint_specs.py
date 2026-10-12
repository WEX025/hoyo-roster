"""Immutable method allowlist for HSR reads and official session initialization."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from .errors import ConfigurationError

HttpMethod = Literal["GET", "POST"]
JsonType = Literal["string", "integer", "number", "boolean", "object", "array", "null"]
SemanticClass = Literal[
    "BUSINESS_READ",
    "PUBLIC_BOOTSTRAP_READ",
    "AUTH_SESSION_INITIALIZATION",
    "DEVICE_FINGERPRINT_INITIALIZATION",
]
BodyField = tuple[str, tuple[JsonType, ...]]


@dataclass(frozen=True, slots=True)
class EndpointSpec:
    name: str
    scheme: Literal["https"]
    host: str
    path: str
    method: HttpMethod
    query_fields: tuple[str, ...]
    body_fields: tuple[BodyField, ...]
    body_required: bool
    allows_auth_cookie: bool
    allows_device_headers: bool
    allows_redirect: Literal[False]
    required_response_fields: tuple[str, ...]
    purpose: str
    semantic_class: SemanticClass

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.host}{self.path}"


def _spec(
    *,
    name: str,
    host: str,
    path: str,
    method: HttpMethod,
    query_fields: tuple[str, ...] = (),
    body_fields: tuple[BodyField, ...] = (),
    body_required: bool = False,
    allows_auth_cookie: bool = True,
    allows_device_headers: bool = False,
    required_response_fields: tuple[str, ...] = (),
    purpose: str,
    semantic_class: SemanticClass,
) -> EndpointSpec:
    return EndpointSpec(
        name=name,
        scheme="https",
        host=host,
        path=path,
        method=method,
        query_fields=query_fields,
        body_fields=body_fields,
        body_required=body_required,
        allows_auth_cookie=allows_auth_cookie,
        allows_device_headers=allows_device_headers,
        allows_redirect=False,
        required_response_fields=required_response_fields,
        purpose=purpose,
        semantic_class=semantic_class,
    )


BINDING = _spec(
    name="binding",
    host="passport-api.mihoyo.com",
    path="/binding/api/getUserGameRolesByLtoken",
    method="GET",
    query_fields=("game_biz",),
    required_response_fields=("data.list",),
    purpose="verify login and select one exact HSR binding",
    semantic_class="BUSINESS_READ",
)
AVATAR_LIST = _spec(
    name="avatar-list",
    host="act-api-takumi.mihoyo.com",
    path="/event/rpgcultivate/avatar/list",
    method="GET",
    query_fields=("badge_region", "badge_uid", "game", "game_biz"),
    allows_device_headers=True,
    required_response_fields=("data.avatars",),
    purpose="read the single-response public avatar collection and owned roster",
    semantic_class="BUSINESS_READ",
)
AVATAR_LIST_BOOTSTRAP = _spec(
    name="avatar-list-bootstrap",
    host="act-api-takumi.mihoyo.com",
    path="/event/rpgcultivate/avatar/list",
    method="GET",
    query_fields=("game",),
    allows_device_headers=True,
    required_response_fields=("data.avatars",),
    purpose="let the official page load its public character collection",
    semantic_class="PUBLIC_BOOTSTRAP_READ",
)
EQUIPMENT_LIST_BOOTSTRAP = _spec(
    name="equipment-list-bootstrap",
    host="act-api-takumi.mihoyo.com",
    path="/event/rpgcultivate/equipment/list",
    method="GET",
    query_fields=("game",),
    allows_device_headers=True,
    required_response_fields=("data.list",),
    purpose="let the official page load its public equipment collection",
    semantic_class="PUBLIC_BOOTSTRAP_READ",
)
ERUDITE_OWNED_LIST_BOOTSTRAP = _spec(
    name="erudite-owned-list-bootstrap",
    host="act-api-takumi.mihoyo.com",
    path="/event/rpgcultivate/eruditewuu/avatar/list",
    method="GET",
    query_fields=("badge_region", "badge_uid", "game", "game_biz"),
    allows_device_headers=True,
    required_response_fields=("data.avatar_list",),
    purpose="let the official page load its current owned-avatar overlay",
    semantic_class="PUBLIC_BOOTSTRAP_READ",
)
AVATAR_DETAIL = _spec(
    name="avatar-detail",
    host="act-api-takumi.mihoyo.com",
    path="/event/rpgcultivate/calc/avatar/detail",
    method="GET",
    query_fields=(
        "badge_region",
        "badge_uid",
        "change_target_level",
        "game",
        "game_biz",
        "item_id",
    ),
    allows_device_headers=True,
    required_response_fields=("data.avatar", "data.equipment"),
    purpose="read one owned avatar's current detail",
    semantic_class="BUSINESS_READ",
)
AVATAR_RECOMMEND = _spec(
    name="avatar-recommend",
    host="act-api-takumi.mihoyo.com",
    path="/event/rpgcultivate/avatar/recommend",
    method="GET",
    query_fields=(
        "badge_region",
        "badge_uid",
        "game",
        "game_biz",
        "is_preview",
        "item_id",
    ),
    allows_device_headers=True,
    purpose="read source-separated current fields when a public plan exists",
    semantic_class="BUSINESS_READ",
)
VERIFY_COOKIE_TOKEN = _spec(
    name="verify-cookie-token",
    host="passport-api.mihoyo.com",
    path="/account/ma-cn-session/web/verifyCookieToken",
    method="POST",
    allows_auth_cookie=True,
    required_response_fields=("data",),
    purpose="let the official page validate its existing CookieToken session",
    semantic_class="AUTH_SESSION_INITIALIZATION",
)
DEVICE_FINGERPRINT = _spec(
    name="device-fingerprint",
    host="public-data-api.mihoyo.com",
    path="/device-fp/api/getFp",
    method="POST",
    body_required=True,
    body_fields=(
        ("app_name", ("string",)),
        ("device_fp", ("string",)),
        ("device_id", ("string",)),
        ("ext_fields", ("string",)),
        ("platform", ("string",)),
        ("seed_id", ("string",)),
        ("seed_time", ("string",)),
    ),
    allows_auth_cookie=True,
    required_response_fields=("data.code", "data.device_fp", "data.msg"),
    purpose="let the official page initialize its transient browser device fingerprint",
    semantic_class="DEVICE_FINGERPRINT_INITIALIZATION",
)

_ENDPOINTS = (
    BINDING,
    AVATAR_LIST,
    AVATAR_LIST_BOOTSTRAP,
    EQUIPMENT_LIST_BOOTSTRAP,
    ERUDITE_OWNED_LIST_BOOTSTRAP,
    AVATAR_DETAIL,
    AVATAR_RECOMMEND,
    VERIFY_COOKIE_TOKEN,
    DEVICE_FINGERPRINT,
)
_REGISTRY = MappingProxyType({item.name: item for item in _ENDPOINTS})

BUSINESS_MUTATION_PATHS = frozenset(
    {
        "/event/rpgcultivate/avatar/read_new",
        "/event/rpgcultivate/eruditewuu/recommend/relic",
        "/event/rpgcultivate/calc/compute",
        "/event/rpgcultivate/recommend/team",
        "/event/rpgcultivate/eruditewuu/recommend/team",
        "/event/rpgcultivate/lineup/like",
    }
)
DEVICE_HEADER_NAMES = frozenset(
    {
        "x-rpc-device_fp",
        "x-rpc-device_id",
        "x-rpc-lang",
        "x-rpc-page",
        "x-rpc-platform",
        "x-rpc-view_source",
    }
)
REQUIRED_DEVICE_HEADER_NAMES = frozenset({"x-rpc-device_fp", "x-rpc-device_id"})


def require_registered_endpoint(value: object) -> EndpointSpec:
    if not isinstance(value, EndpointSpec) or _REGISTRY.get(value.name) is not value:
        raise ConfigurationError("endpoint is not registered")
    if value.scheme != "https" or value.allows_redirect:
        raise ConfigurationError("registered endpoint violates the network boundary")
    return value


def _json_type(value: object) -> JsonType:
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) is int:
        return "integer"
    if type(value) is float:
        return "number"
    if type(value) is str:
        return "string"
    if type(value) is list:
        return "array"
    if type(value) is dict:
        return "object"
    raise ConfigurationError("request body contained an unsupported JSON type")


def validate_body(endpoint: EndpointSpec, body: object) -> None:
    spec = require_registered_endpoint(endpoint)
    if spec.method == "GET":
        if body is not None:
            raise ConfigurationError(f"{spec.name}: GET body was forbidden")
        return
    if body is None:
        if spec.body_required:
            raise ConfigurationError(f"{spec.name}: request body was required")
        body = {}
    if not isinstance(body, dict):
        raise ConfigurationError(f"{spec.name}: request body was invalid")
    expected = {name: types for name, types in spec.body_fields}
    if set(body) != set(expected):
        raise ConfigurationError(f"{spec.name}: request body fields were invalid")
    for name, value in body.items():
        if _json_type(value) not in expected[name]:
            raise ConfigurationError(f"{spec.name}: request body field type was invalid")


def match_registered_endpoint(
    *,
    scheme: str,
    host: str,
    path: str,
    method: str,
    query_fields: set[str],
    body: object = None,
) -> EndpointSpec:
    matches = [
        endpoint
        for endpoint in _ENDPOINTS
        if endpoint.scheme == scheme
        and endpoint.host == host
        and endpoint.path == path
        and endpoint.method == method
        and set(endpoint.query_fields) == query_fields
    ]
    if len(matches) != 1:
        raise ConfigurationError("request did not match one registered endpoint")
    endpoint = require_registered_endpoint(matches[0])
    validate_body(endpoint, body)
    return endpoint


def response_has_required_fields(endpoint: EndpointSpec, payload: object) -> bool:
    spec = require_registered_endpoint(endpoint)
    if not isinstance(payload, dict):
        return False
    for dotted in spec.required_response_fields:
        current: Any = payload
        for part in dotted.split("."):
            if not isinstance(current, dict) or part not in current:
                return False
            current = current[part]
    return True
