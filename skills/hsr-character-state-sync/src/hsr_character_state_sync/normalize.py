"""Fail-closed source validation and HSR partial Canonical mapping."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import (
    ApiResponseError,
    CultivationOwnedRosterEmpty,
    DetailCoverageError,
    ExpectedUidAmbiguous,
    ExpectedUidNotFound,
    RosterCompletenessError,
    UnsupportedMultiPageContract,
)
from .model import Snapshot, SnapshotDiagnostics

EXPECTED_GAME_BIZ = "hkrpg_cn"
EXPECTED_REGION = "prod_gf_cn"
_MAX_ID_DIGITS = 32
_STABLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_PAGINATION_FIELDS = {
    "total",
    "page",
    "cursor",
    "offset",
    "limit",
    "has_more",
    "page_info",
}
_SKILL_ANCHORS = {
    "Point01": "basic_level",
    "Point02": "skill_level",
    "Point03": "ultimate_level",
    "Point04": "talent_level",
}
_EXTRA_TOP_LEVEL_ANCHORS = {"Point19", "Point20", "Point21", "Point22"}
_TOP_LEVEL_ANCHORS = {*_SKILL_ANCHORS, *_EXTRA_TOP_LEVEL_ANCHORS}
_CONFIRMED_TRACE_TYPES = {"TraceNormal1", "TracePrimary3"}
_SKILL_CONTAINERS = ("skills", "skills_other", "skills_servant", "skills_special")
_RELIC_SLOTS = {
    "HEAD": "head",
    "HAND": "hands",
    "BODY": "body",
    "FOOT": "feet",
    "SIDEBALL": "planar_sphere",
    "ROPE": "link_rope",
}
_EMPTY_RELIC_SENTINEL = {
    "item_id": 0,
    "display_item_id": 0,
    "set_id": 0,
    "main_property": -999,
    "level": 0,
}
_PANEL_FIELDS = {
    "1": "hp",
    "2": "attack",
    "3": "defense",
    "4": "speed",
    "5": "crit_rate",
    "6": "crit_damage",
    "7": "break_effect",
    "8": "effect_hit_rate",
    "9": "effect_res",
}


def positive_id(value: object, *, label: str = "entity ID") -> str:
    if isinstance(value, bool):
        raise ApiResponseError(f"{label} was invalid")
    if isinstance(value, int):
        if value < 1 or len(str(value)) > _MAX_ID_DIGITS:
            raise ApiResponseError(f"{label} was invalid")
        return str(value)
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdigit()
        or value.startswith("0")
        or len(value) > _MAX_ID_DIGITS
    ):
        raise ApiResponseError(f"{label} was invalid")
    return value


def stable_id(value: object, *, label: str) -> str:
    if isinstance(value, bool):
        raise ApiResponseError(f"{label} was invalid")
    if isinstance(value, int):
        if value < 1:
            raise ApiResponseError(f"{label} was invalid")
        return str(value)
    if (
        not isinstance(value, str)
        or not value
        or value == "0"
        or _STABLE_ID_RE.fullmatch(value) is None
    ):
        raise ApiResponseError(f"{label} was invalid")
    return value


def nonnegative_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ApiResponseError(f"{label} was invalid")
    return value


def nonempty_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ApiResponseError(f"{label} was invalid")
    return value


def decimal_number(value: object, *, label: str) -> Decimal:
    if isinstance(value, bool | float):
        raise ApiResponseError(f"{label} was not an exact decimal")
    if not isinstance(value, str | int | Decimal):
        raise ApiResponseError(f"{label} was invalid")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ApiResponseError(f"{label} was invalid") from None
    if not result.is_finite() or result < 0:
        raise ApiResponseError(f"{label} was invalid")
    return result


def select_binding(data: Mapping[str, Any], uid: object) -> dict[str, Any]:
    expected_uid = positive_id(uid, label="expected UID")
    roles = data.get("list")
    if not isinstance(roles, list):
        raise ApiResponseError("binding list was invalid")
    matches: list[dict[str, Any]] = []
    for raw in roles:
        if not isinstance(raw, dict) or raw.get("game_biz") != EXPECTED_GAME_BIZ:
            continue
        try:
            role_uid = positive_id(raw.get("game_uid"), label="binding UID")
            region = nonempty_string(raw.get("region"), label="binding region")
        except ApiResponseError:
            continue
        if role_uid == expected_uid and region == EXPECTED_REGION:
            matches.append(raw)
    if not matches:
        raise ExpectedUidNotFound("expected HSR UID and region were not found")
    if len(matches) != 1:
        raise ExpectedUidAmbiguous("expected HSR UID and region were ambiguous")
    role = matches[0]
    return {
        "uid": expected_uid,
        "server": EXPECTED_REGION,
        "nickname": nonempty_string(role.get("nickname"), label="binding nickname"),
        "player_level": nonnegative_int(role.get("level"), label="binding player level"),
    }


def _contains_pagination_signal(value: object) -> bool:
    if isinstance(value, Mapping):
        if any(key in _PAGINATION_FIELDS for key in value):
            return True
        return any(_contains_pagination_signal(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_pagination_signal(child) for child in value)
    return False


def _keyset_digest(values: Sequence[Mapping[str, Any]]) -> str:
    keysets = sorted(",".join(sorted(value)) for value in values)
    return hashlib.sha256("\n".join(keysets).encode("utf-8")).hexdigest()


def validate_owned_roster(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    if _contains_pagination_signal(data):
        raise UnsupportedMultiPageContract("avatar list exposed an unsupported paging signal")
    raw_avatars = data.get("avatars")
    if not isinstance(raw_avatars, list):
        raise ApiResponseError("avatar list was invalid")
    identifiers: set[str] = set()
    owned: list[dict[str, Any]] = []
    false_count = 0
    for raw in raw_avatars:
        if not isinstance(raw, dict):
            raise ApiResponseError("avatar entry was invalid")
        identifier = positive_id(raw.get("item_id"), label="character ID")
        if identifier in identifiers:
            raise RosterCompletenessError("character IDs were not unique")
        identifiers.add(identifier)
        is_owned = raw.get("is_own")
        if type(is_owned) is not bool:
            raise ApiResponseError("avatar ownership flag was invalid")
        if is_owned:
            nonempty_string(raw.get("item_name"), label="character name")
            nonnegative_int(raw.get("cur_level"), label="character level")
            owned.append(raw)
        else:
            false_count += 1
    if not owned:
        raise CultivationOwnedRosterEmpty(
            entry_count=len(raw_avatars),
            true_count=0,
            false_count=false_count,
            response_data_keyset_sha256=_keyset_digest([data]),
            entry_keyset_sha256=_keyset_digest(raw_avatars),
        )
    return sorted(owned, key=lambda item: positive_id(item["item_id"], label="character ID"))


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ApiResponseError(f"{label} was invalid")
    return value


def _sequence(value: object, *, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ApiResponseError(f"{label} was invalid")
    return value


def _detail_points(detail: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for container in _SKILL_CONTAINERS:
        raw_points = detail.get(container, [])
        for raw in _sequence(raw_points, label=f"detail {container}"):
            point = _mapping(raw, label="detail point")
            point_id = stable_id(point.get("point_id"), label="point ID")
            if point_id in seen:
                raise ApiResponseError("detail point IDs were not unique")
            seen.add(point_id)
            result.append(point)
    return result


def _recommend_rank_add(recommend: Mapping[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    top_level = recommend.get("rank_add")
    if top_level is not None:
        values = _mapping(top_level, label="recommend rank_add")
        for anchor, value in values.items():
            if anchor in _SKILL_ANCHORS:
                result[anchor] = nonnegative_int(value, label="skill rank_add")
    for container in _SKILL_CONTAINERS:
        raw_points = recommend.get(container)
        if raw_points is None:
            continue
        for raw in _sequence(raw_points, label=f"recommend {container}"):
            point = _mapping(raw, label="recommend point")
            if "rank_add" not in point:
                continue
            anchor = nonempty_string(point.get("anchor"), label="recommend point anchor")
            if anchor not in _SKILL_ANCHORS:
                continue
            value = nonnegative_int(point["rank_add"], label="skill rank_add")
            if anchor in result and result[anchor] != value:
                raise ApiResponseError("skill rank_add sources disagreed")
            result[anchor] = value
    return result


def _progression(
    detail: Mapping[str, Any],
    recommend: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, set[str], int]:
    points = _detail_points(detail)
    base_levels: dict[str, int] = {}
    traces: dict[str, dict[str, int]] = {}
    extras: set[str] = set()
    unmapped = 0
    for point in points:
        point_id = stable_id(point.get("point_id"), label="point ID")
        anchor = nonempty_string(point.get("anchor"), label="point anchor")
        level = nonnegative_int(point.get("cur_level"), label="point current level")
        point_type = nonempty_string(point.get("point_type"), label="point type")
        if anchor in _SKILL_ANCHORS:
            if anchor in base_levels:
                raise ApiResponseError("ordinary skill anchors were not unique")
            base_levels[anchor] = level
            continue
        if anchor in _EXTRA_TOP_LEVEL_ANCHORS:
            extras.add(anchor)
            unmapped += 1
            continue
        if anchor in _TOP_LEVEL_ANCHORS:
            unmapped += 1
            continue
        if point_type in _CONFIRMED_TRACE_TYPES:
            traces[point_id] = {"level": level}
        else:
            unmapped += 1
    progression: dict[str, Any] = {}
    if recommend is not None:
        rank_add = _recommend_rank_add(recommend)
        if set(rank_add) == set(_SKILL_ANCHORS) and set(base_levels) == set(_SKILL_ANCHORS):
            for anchor, canonical in _SKILL_ANCHORS.items():
                progression[canonical] = base_levels[anchor] + rank_add[anchor]
    if traces:
        progression["trace_nodes"] = {key: traces[key] for key in sorted(traces)}
    return (progression or None), extras, unmapped


def _light_cone(detail: Mapping[str, Any]) -> dict[str, Any] | None:
    if "equipment" not in detail:
        raise ApiResponseError("detail equipment coverage was missing")
    raw = detail["equipment"]
    if raw is None:
        return None
    equipment = _mapping(raw, label="detail equipment")
    return {
        "light_cone_id": positive_id(equipment.get("item_id"), label="light cone ID"),
        "name": nonempty_string(equipment.get("item_name"), label="light cone name"),
        "rarity": nonnegative_int(equipment.get("rarity"), label="light cone rarity"),
        "level": nonnegative_int(equipment.get("cur_level"), label="light cone level"),
    }


def _cross_confirm_recommend(
    detail: Mapping[str, Any],
    recommend: Mapping[str, Any] | None,
) -> None:
    if recommend is None or "equipment" not in recommend:
        return
    detail_equipment = detail.get("equipment")
    recommend_equipment = recommend["equipment"]
    if detail_equipment is None or recommend_equipment is None:
        if detail_equipment is not recommend_equipment:
            raise ApiResponseError("current equipment sources disagreed")
        return
    left = _mapping(detail_equipment, label="detail equipment")
    right = _mapping(recommend_equipment, label="recommend equipment")
    for field in ("item_id", "item_name", "rarity", "cur_level"):
        if field in right and right[field] != left.get(field):
            raise ApiResponseError("current equipment sources disagreed")


def _relics(
    recommend: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, int, int]:
    if recommend is None or "relics_v2" not in recommend:
        return None, 0, 0
    relics_v2 = _mapping(recommend["relics_v2"], label="recommend relics_v2")
    if "dress_relics" not in relics_v2:
        raise ApiResponseError("recommend relic slot coverage was missing")
    slots: dict[str, Any] = {}
    equipped = 0
    empty = 0
    for raw in _sequence(relics_v2["dress_relics"], label="recommend dress_relics"):
        relic = _mapping(raw, label="recommend relic")
        body_type = nonempty_string(relic.get("body_type"), label="relic body type")
        canonical_slot = _RELIC_SLOTS.get(body_type)
        if canonical_slot is None:
            raise ApiResponseError("relic body type was unknown")
        if canonical_slot in slots:
            raise ApiResponseError("relic slots were not unique")
        sentinel_matches = {
            field: relic.get(field) == expected
            for field, expected in _EMPTY_RELIC_SENTINEL.items()
        }
        if all(sentinel_matches.values()):
            slots[canonical_slot] = None
            empty += 1
            continue
        if any(sentinel_matches.values()):
            raise ApiResponseError("relic empty-slot sentinel was inconsistent")
        positive_id(relic.get("item_id"), label="relic item ID")
        positive_id(relic.get("display_item_id"), label="relic display item ID")
        slots[canonical_slot] = {
            "set_id": positive_id(relic.get("set_id"), label="relic set ID"),
            "set_name": nonempty_string(relic.get("set_name"), label="relic set name"),
            "rarity": nonnegative_int(relic.get("rarity"), label="relic rarity"),
            "level": nonnegative_int(relic.get("level"), label="relic level"),
        }
        equipped += 1
    ordered = {
        slot: slots[slot]
        for slot in (
            "head",
            "hands",
            "body",
            "feet",
            "planar_sphere",
            "link_rope",
        )
        if slot in slots
    }
    return (ordered or None), equipped, empty


def _panel(
    recommend: Mapping[str, Any] | None,
) -> tuple[dict[str, Decimal] | None, set[str]]:
    if recommend is None or "avatar_property_v2" not in recommend:
        return None, set()
    observed: set[str] = set()
    panel: dict[str, Decimal] = {}
    for raw in _sequence(
        recommend["avatar_property_v2"], label="recommend avatar_property_v2"
    ):
        prop = _mapping(raw, label="recommend panel property")
        property_id = positive_id(prop.get("property_type"), label="panel property ID")
        if property_id in observed:
            raise ApiResponseError("panel property IDs were not unique")
        observed.add(property_id)
        is_percent = prop.get("is_percent")
        if type(is_percent) is not bool:
            raise ApiResponseError("panel percentage flag was invalid")
        values = _sequence(prop.get("values"), label="panel property values")
        if not values:
            raise ApiResponseError("panel property values were empty")
        current = Decimal(0)
        for raw_value in values:
            value = _mapping(raw_value, label="panel property value")
            if "cur_num" not in value:
                raise ApiResponseError("panel current value was missing")
            current += decimal_number(value["cur_num"], label="panel current value")
        canonical = _PANEL_FIELDS.get(property_id)
        if canonical is not None:
            panel[canonical] = current / Decimal(100) if is_percent else current
    return (panel or None), observed


def normalize_profile(
    binding: Mapping[str, Any],
    *,
    synced_at: datetime | None = None,
) -> dict[str, Any]:
    timestamp = (synced_at or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    return {
        "schema_version": "game-profile/v1",
        "game": "honkai-star-rail",
        "account": dict(binding),
        "sync": {
            "state": "partial",
            "source": "miyoushe-web",
            "synced_at": timestamp.isoformat().replace("+00:00", "Z"),
        },
    }


def normalize_snapshot(
    binding: Mapping[str, Any],
    avatars: list[dict[str, Any]],
    details: Mapping[str, Mapping[str, Any]],
    recommendations: Mapping[str, Mapping[str, Any]],
    *,
    synced_at: datetime | None = None,
) -> Snapshot:
    roster_ids = {
        positive_id(raw.get("item_id"), label="character ID") for raw in avatars
    }
    if set(details) != roster_ids:
        raise DetailCoverageError("detail coverage did not equal the owned roster")
    if not set(recommendations) <= roster_ids:
        raise ApiResponseError("recommend coverage was outside the owned roster")
    profile = normalize_profile(binding, synced_at=synced_at)
    roster_items: dict[str, dict[str, Any]] = {}
    builds: dict[str, dict[str, Any]] = {}
    light_cone_equipped = 0
    light_cone_unequipped = 0
    relic_equipped = 0
    relic_empty = 0
    panel_characters = 0
    observed_properties: set[str] = set()
    extra_points: set[str] = set()
    unmapped_points = 0
    for raw in avatars:
        character_id = positive_id(raw.get("item_id"), label="character ID")
        detail = _mapping(details[character_id], label="avatar detail")
        detail_avatar = _mapping(detail.get("avatar"), label="detail avatar")
        response_id = positive_id(detail_avatar.get("item_id"), label="detail character ID")
        if response_id != character_id:
            raise DetailCoverageError("detail request and response IDs disagreed")
        recommend = recommendations.get(character_id)
        if recommend is not None:
            recommend_avatar = recommend.get("avatar")
            if recommend_avatar is not None:
                recommend_id = positive_id(
                    _mapping(recommend_avatar, label="recommend avatar").get("item_id"),
                    label="recommend character ID",
                )
                if recommend_id != character_id:
                    raise DetailCoverageError("recommend request and response IDs disagreed")
        _cross_confirm_recommend(detail, recommend)
        roster_items[character_id] = {
            "name": nonempty_string(raw.get("item_name"), label="character name"),
            "level": nonnegative_int(raw.get("cur_level"), label="character level"),
        }
        build: dict[str, Any] = {
            "schema_version": "honkai-star-rail-build/v1",
            "game": "honkai-star-rail",
        }
        progression, extras, unmapped = _progression(detail, recommend)
        if progression is not None:
            build["progression"] = progression
        extra_points.update(extras)
        unmapped_points += unmapped
        light_cone = _light_cone(detail)
        if light_cone is None:
            light_cone_unequipped += 1
        else:
            build["light_cone"] = light_cone
            light_cone_equipped += 1
        relics, equipped_count, empty_count = _relics(recommend)
        if relics is not None:
            build["relics"] = relics
        relic_equipped += equipped_count
        relic_empty += empty_count
        panel, property_ids = _panel(recommend)
        observed_properties.update(property_ids)
        if panel is not None:
            build["panel_context"] = "out_of_combat"
            build["panel"] = panel
            panel_characters += 1
        builds[character_id] = build
    ordered_ids = sorted(roster_items)
    diagnostics = SnapshotDiagnostics(
        detail_count=len(details),
        recommend_count=len(recommendations),
        light_cone_equipped_count=light_cone_equipped,
        light_cone_unequipped_count=light_cone_unequipped,
        relic_equipped_slot_count=relic_equipped,
        relic_empty_slot_count=relic_empty,
        panel_character_count=panel_characters,
        observed_panel_property_ids=tuple(sorted(observed_properties)),
        observed_extra_top_level_point_types=tuple(sorted(extra_points)),
        unmapped_public_point_count=unmapped_points,
    )
    return Snapshot(
        profile=profile,
        roster={
            "schema_version": "honkai-star-rail-roster/v1",
            "game": "honkai-star-rail",
            "characters": {
                character_id: roster_items[character_id] for character_id in ordered_ids
            },
        },
        builds={character_id: builds[character_id] for character_id in ordered_ids},
        diagnostics=diagnostics,
        source_coverage={
            "detail_ids": sorted(details),
            "recommend_ids": sorted(recommendations),
            "observed_panel_property_ids": sorted(observed_properties),
            "observed_extra_top_level_point_types": sorted(extra_points),
            "unmapped_public_point_count": unmapped_points,
        },
    )
