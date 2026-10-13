"""Fixed, read-only ZZZ collection sequence with completeness proofs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .endpoint_specs import (
    AVATAR_BASIC,
    AVATAR_INFO,
    BADGE_LOGIN,
    BINDING,
    BUDDY_INFO,
    CULTIVATE_AVATAR_BASIC,
    CULTIVATE_BATCH_DETAIL,
    EndpointSpec,
)
from .errors import (
    ApiResponseError,
    AuthenticationError,
    ConfigurationError,
    SnapshotConsistencyError,
    UidRequiredError,
)
from .ids import parse_api_character_id, parse_bangboo_id, parse_character_id, parse_role_uid
from .pagination import (
    CollectionResult,
    CompletenessAuthority,
    EntityId,
    collect_complete_collection,
)

ROSTER_COMPLETENESS_AUTHORITY: CompletenessAuthority = "OFFICIAL_FRONTEND_SINGLE_RESPONSE_CLOSURE"
CULTIVATE_COMPLETENESS_AUTHORITY: CompletenessAuthority = (
    "OFFICIAL_FRONTEND_SINGLE_RESPONSE_CLOSURE"
)
BANGBOO_COMPLETENESS_AUTHORITY: CompletenessAuthority = "OFFICIAL_FRONTEND_SINGLE_RESPONSE_CLOSURE"


class Transport(Protocol):
    async def request_json(
        self,
        endpoint: EndpointSpec,
        *,
        params: Mapping[str, object] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def has_cookie(self, name: str) -> bool: ...

    async def clear_cultivate_cookies(self) -> None: ...


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ApiResponseError(f"{label}: expected an object")
    return value


def _sequence(value: object, *, label: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise ApiResponseError(f"{label}: expected a list")
    return value


def _entity_id(value: Mapping[str, Any], *, label: str) -> str:
    try:
        return parse_api_character_id(value.get("id"))
    except ApiResponseError:
        raise ApiResponseError(f"{label}: entity ID was invalid") from None


def _bangboo_id(value: Mapping[str, Any]) -> str:
    try:
        return parse_bangboo_id(value.get("id"), error_type=ApiResponseError)
    except ApiResponseError:
        raise ApiResponseError("buddy/info item: entity ID was invalid") from None


def _cultivate_id(value: Mapping[str, Any]) -> str:
    avatar = _mapping(value.get("avatar"), label="cultivate basic avatar")
    try:
        return parse_character_id(avatar.get("id"), error_type=ApiResponseError)
    except ApiResponseError:
        raise ApiResponseError("cultivate basic avatar: entity ID was invalid") from None


def avatar_ids(data: Mapping[str, Any], *, field: str = "avatar_list") -> set[str]:
    items = _sequence(data.get(field), label=field)
    result = {
        _entity_id(_mapping(item, label=f"{field} item"), label=f"{field} item") for item in items
    }
    if len(result) != len(items):
        raise SnapshotConsistencyError(f"{field}: duplicate entity ID")
    return result


def cultivate_owned_ids(data: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()
    for raw_item in _sequence(data.get("list"), label="cultivate basic list"):
        item = _mapping(raw_item, label="cultivate basic item")
        unlocked = item.get("unlocked")
        is_teaser = item.get("is_teaser")
        if not isinstance(unlocked, bool) or not isinstance(is_teaser, bool):
            raise ApiResponseError("cultivate ownership flags were invalid")
        if unlocked and is_teaser:
            raise SnapshotConsistencyError("cultivate ownership flags were contradictory")
        if not unlocked or is_teaser:
            continue
        character_id = _cultivate_id(item)
        if character_id in result:
            raise SnapshotConsistencyError("cultivate basic repeated a character")
        result.add(character_id)
    return result


@dataclass(frozen=True, slots=True)
class Role:
    uid: str
    server: str
    nickname: str
    player_level: int


@dataclass(frozen=True, slots=True)
class CompletenessEvidence:
    roster_authority: str
    roster_pagination_mode: str
    roster_total: int
    cultivate_authority: str
    cultivate_pagination_mode: str
    cultivate_total: int
    bangboo_authority: str
    bangboo_pagination_mode: str
    bangboo_total: int


@dataclass(frozen=True, slots=True)
class CollectedState:
    role: Role
    basic: Mapping[str, Any]
    cultivate_basic: Mapping[str, Any]
    character_details: Mapping[str, Mapping[str, Any]]
    cultivate_details: Mapping[str, Mapping[str, Any]]
    bangboos: Mapping[str, Any]
    completeness: CompletenessEvidence


def select_role(binding: Mapping[str, Any], uid: str | None) -> Role:
    """Select one nap_cn role, never defaulting among multiple accounts."""

    raw_roles = _sequence(binding.get("list"), label="binding list")
    roles: list[Role] = []
    for raw_role in raw_roles:
        item = _mapping(raw_role, label="binding role")
        if item.get("game_biz") != "nap_cn":
            continue
        role_uid_raw = item.get("game_uid")
        server = item.get("region")
        nickname = item.get("nickname")
        level = item.get("level")
        if (
            not isinstance(server, str)
            or not server
            or not isinstance(nickname, str)
            or not nickname
            or not isinstance(level, int)
            or isinstance(level, bool)
            or level < 0
        ):
            raise ApiResponseError("binding role: identity fields were incomplete")
        role_uid = parse_role_uid(role_uid_raw, error_type=ApiResponseError)
        roles.append(Role(role_uid, server, nickname, level))
    if not roles:
        raise ConfigurationError("no nap_cn role is available for the logged-in account")
    if uid is None and len(roles) > 1:
        raise UidRequiredError("--uid is required when multiple nap_cn roles are present")
    if uid is not None:
        selected_uid = parse_role_uid(uid, error_type=ConfigurationError)
        matches = [role for role in roles if role.uid == selected_uid]
        if len(matches) != 1:
            raise ConfigurationError("--uid did not match exactly one nap_cn role")
        return matches[0]
    return roles[0]


class EndpointClient:
    """Call only registered EndpointSpecs in the fixed business collection flow."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport
        self._cultivate_login_attempted = False

    async def get_binding(self) -> Mapping[str, Any]:
        return await self._transport.request_json(
            BINDING,
            params={"game_biz": "nap_cn"},
        )

    async def _record(
        self,
        endpoint: EndpointSpec,
        role: Role,
        *,
        extra_params: Mapping[str, object] | None = None,
    ) -> Mapping[str, Any]:
        params: dict[str, object] = {"role_id": role.uid, "server": role.server}
        params.update(extra_params or {})
        return await self._transport.request_json(endpoint, params=params)

    async def _cultivate_login(self, role: Role) -> None:
        if self._cultivate_login_attempted:
            raise AuthenticationError("cultivate login refresh was already attempted")
        self._cultivate_login_attempted = True
        await self._transport.clear_cultivate_cookies()
        await self._transport.request_json(
            BADGE_LOGIN,
            body={
                "game_biz": "nap_cn",
                "lang": "zh-cn",
                "region": role.server,
                "uid": role.uid,
            },
        )
        if not await self._transport.has_cookie("e_nap_token"):
            raise AuthenticationError("cultivate login did not create e_nap_token")

    async def _cultivate(
        self,
        endpoint: EndpointSpec,
        role: Role,
        *,
        extra_params: Mapping[str, object] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        if not await self._transport.has_cookie("e_nap_token"):
            await self._cultivate_login(role)
        params: dict[str, object] = {"uid": role.uid, "region": role.server}
        params.update(extra_params or {})
        try:
            return await self._transport.request_json(
                endpoint,
                params=params,
                body=body,
            )
        except AuthenticationError:
            await self._cultivate_login(role)
            return await self._transport.request_json(
                endpoint,
                params=params,
                body=body,
            )

    async def _complete_record_collection(
        self,
        endpoint: EndpointSpec,
        role: Role,
        *,
        label: str,
        list_field: str,
        entity_id: EntityId,
        authority: CompletenessAuthority,
    ) -> CollectionResult:
        async def fetch_page(page_params: Mapping[str, object]) -> Mapping[str, Any]:
            return await self._record(endpoint, role, extra_params=page_params)

        return await collect_complete_collection(
            fetch_page,
            label=label,
            list_field=list_field,
            entity_id=entity_id,
            single_response_authority=authority,
            pagination_contract=endpoint.pagination_contract,
        )

    async def _complete_cultivate_collection(self, role: Role) -> CollectionResult:
        async def fetch_page(page_params: Mapping[str, object]) -> Mapping[str, Any]:
            return await self._cultivate(
                CULTIVATE_AVATAR_BASIC,
                role,
                extra_params=page_params,
            )

        return await collect_complete_collection(
            fetch_page,
            label="cultivate avatar_basic_list",
            list_field="list",
            entity_id=_cultivate_id,
            single_response_authority=CULTIVATE_COMPLETENESS_AUTHORITY,
            pagination_contract=CULTIVATE_AVATAR_BASIC.pagination_contract,
        )

    async def collect_from_binding(
        self,
        binding: Mapping[str, Any],
        *,
        uid: str | None,
    ) -> CollectedState:
        """Run one complete serial business pass from its own binding response."""

        role = select_role(binding, uid)
        basic_result = await self._complete_record_collection(
            AVATAR_BASIC,
            role,
            label="avatar/basic",
            list_field="avatar_list",
            entity_id=lambda item: _entity_id(item, label="avatar/basic item"),
            authority=ROSTER_COMPLETENESS_AUTHORITY,
        )
        if not basic_result.ids:
            raise SnapshotConsistencyError("avatar/basic returned an empty owned roster")

        cultivate_result = await self._complete_cultivate_collection(role)
        owned_cultivate_ids = cultivate_owned_ids(cultivate_result.data)
        if set(basic_result.ids) != owned_cultivate_ids:
            raise SnapshotConsistencyError(
                "avatar/basic and cultivate owned rosters were not exactly equal"
            )

        character_details: dict[str, Mapping[str, Any]] = {}
        for character_id in sorted(basic_result.ids):
            detail_data = await self._record(
                AVATAR_INFO,
                role,
                extra_params={"id_list[]": character_id},
            )
            detail_list = _sequence(detail_data.get("avatar_list"), label="avatar/info list")
            if len(detail_list) != 1:
                raise SnapshotConsistencyError("avatar/info did not return exactly one entity")
            detail = _mapping(detail_list[0], label="avatar/info entity")
            if _entity_id(detail, label="avatar/info entity") != character_id:
                raise SnapshotConsistencyError("avatar/info entity ID did not match the request")
            character_details[character_id] = detail

        cultivate_details: dict[str, Mapping[str, Any]] = {}
        ordered_ids = sorted(basic_result.ids)
        for offset in range(0, len(ordered_ids), 10):
            batch = ordered_ids[offset : offset + 10]
            body = {
                "avatar_list": [
                    {
                        "avatar_id": int(character_id),
                        "is_teaser": False,
                        "teaser_need_weapon": False,
                        "teaser_sp_skill": False,
                    }
                    for character_id in batch
                ]
            }
            batch_data = await self._cultivate(
                CULTIVATE_BATCH_DETAIL,
                role,
                body=body,
            )
            batch_ids: set[str] = set()
            for raw_item in _sequence(batch_data.get("list"), label="cultivate detail list"):
                item = _mapping(raw_item, label="cultivate detail")
                avatar = _mapping(item.get("avatar"), label="cultivate detail avatar")
                character_id = _entity_id(avatar, label="cultivate detail avatar")
                if (
                    character_id not in batch
                    or character_id in batch_ids
                    or character_id in cultivate_details
                ):
                    raise SnapshotConsistencyError("cultivate detail entity ID was unexpected")
                batch_ids.add(character_id)
                cultivate_details[character_id] = item
            if set(batch) != batch_ids:
                raise SnapshotConsistencyError("cultivate detail batch was incomplete")

        bangboo_result = await self._complete_record_collection(
            BUDDY_INFO,
            role,
            label="buddy/info",
            list_field="list",
            entity_id=_bangboo_id,
            authority=BANGBOO_COMPLETENESS_AUTHORITY,
        )
        return CollectedState(
            role=role,
            basic=basic_result.data,
            cultivate_basic=cultivate_result.data,
            character_details=character_details,
            cultivate_details=cultivate_details,
            bangboos=bangboo_result.data,
            completeness=CompletenessEvidence(
                roster_authority=basic_result.authority,
                roster_pagination_mode=basic_result.pagination_mode,
                roster_total=basic_result.total,
                cultivate_authority=cultivate_result.authority,
                cultivate_pagination_mode=cultivate_result.pagination_mode,
                cultivate_total=len(owned_cultivate_ids),
                bangboo_authority=bangboo_result.authority,
                bangboo_pagination_mode=bangboo_result.pagination_mode,
                bangboo_total=bangboo_result.total,
            ),
        )
