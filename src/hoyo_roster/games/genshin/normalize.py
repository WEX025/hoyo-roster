"""Fail-closed source validation and partial Canonical mapping."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from .errors import (
    ApiResponseError,
    ExpectedUidAmbiguous,
    ExpectedUidNotFound,
    RosterCompletenessError,
    UnsupportedMultiPageContract,
)
from .model import Snapshot

EXPECTED_GAME_BIZ = "hk4e_cn"
_MAX_ID_DIGITS = 32
TRAVELER_IDS = frozenset({"10000005", "10000007"})
# Proven by the first-party Miyoushe cultivation calculator configuration.
ELEMENT_ATTR_TO_CANONICAL = {
    1: "pyro",
    2: "anemo",
    3: "geo",
    4: "dendro",
    5: "electro",
    6: "hydro",
    7: "cryo",
}
CORE_SKILL_FIELDS = (
    "normal_attack_level",
    "elemental_skill_level",
    "elemental_burst_level",
)


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


def nonnegative_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ApiResponseError(f"{label} was invalid")
    return value


def bounded_int(value: object, *, label: str, minimum: int, maximum: int) -> int:
    result = nonnegative_int(value, label=label)
    if result < minimum or result > maximum:
        raise ApiResponseError(f"{label} was invalid")
    return result


def nonempty_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ApiResponseError(f"{label} was invalid")
    return value


def select_binding(data: Mapping[str, Any], uid: str) -> dict[str, Any]:
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
        except ApiResponseError:
            continue
        if role_uid == expected_uid:
            matches.append(raw)
    if not matches:
        raise ExpectedUidNotFound("expected Genshin UID was not found")
    if len(matches) != 1:
        raise ExpectedUidAmbiguous("expected Genshin UID was ambiguous")
    role = matches[0]
    return {
        "uid": expected_uid,
        "server": nonempty_string(role.get("region"), label="binding region"),
        "nickname": nonempty_string(role.get("nickname"), label="binding nickname"),
        "player_level": nonnegative_int(role.get("level"), label="binding player level"),
    }


def validate_complete_list(data: Mapping[str, Any], *, request_size: int) -> list[dict[str, Any]]:
    pagination_signals = {
        "hasmore",
        "hasnext",
        "cursor",
        "nextcursor",
        "pagination",
        "paging",
        "pager",
        "pageinfo",
        "pagesize",
        "nextpage",
        "offset",
        "page",
        "pagenum",
        "limit",
        "next",
        "islast",
        "totalcount",
    }
    if any(
        "".join(character for character in str(key).lower() if character.isalnum())
        in pagination_signals
        for key in data
    ):
        raise UnsupportedMultiPageContract("unregistered pagination signals in source response")
    raw_list = data.get("list")
    total = data.get("total")
    if not isinstance(raw_list, list) or isinstance(total, bool) or not isinstance(total, int):
        raise ApiResponseError("cultivation list closure fields were invalid")
    if total < 0:
        raise ApiResponseError("cultivation total was invalid")
    if total > request_size or len(raw_list) < total:
        raise UnsupportedMultiPageContract("single response did not close the total")
    if len(raw_list) != total:
        raise RosterCompletenessError("list length did not equal total")
    if total == 0:
        raise RosterCompletenessError("owned roster was empty")
    result: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for raw in raw_list:
        if not isinstance(raw, dict):
            raise ApiResponseError("avatar entry was invalid")
        identifier = positive_id(raw.get("id"), label="character ID")
        if identifier in identifiers:
            raise RosterCompletenessError("character IDs were not unique")
        identifiers.add(identifier)
        result.append(raw)
    return result


def _weapon(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    if "weapon" not in raw:
        raise ApiResponseError("avatar weapon coverage was missing")
    value = raw["weapon"]
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ApiResponseError("avatar weapon was invalid")
    return {
        "weapon_id": positive_id(value.get("id"), label="weapon ID"),
        "name": nonempty_string(value.get("name"), label="weapon name"),
        "rarity": nonnegative_int(value.get("weapon_level"), label="weapon rarity"),
        "level": nonnegative_int(value.get("level_current"), label="weapon level"),
    }


def _traveler_element(raw: Mapping[str, Any]) -> tuple[str, int]:
    source_id = nonnegative_int(raw.get("element_attr_id"), label="traveler element")
    try:
        return ELEMENT_ATTR_TO_CANONICAL[source_id], source_id
    except KeyError as error:
        raise ApiResponseError("traveler element was unrecognized") from error


def _progression(
    raw: Mapping[str, Any], *, character_id: str
) -> tuple[dict[str, int], dict[str, dict[str, int | str]]]:
    skills = raw.get("skill_list")
    if not isinstance(skills, list):
        raise ApiResponseError("avatar skill coverage was missing")
    suffixes = {
        30 if character_id in TRAVELER_IDS else 31: "normal_attack_level",
        32: "elemental_skill_level",
        39: "elemental_burst_level",
    }
    levels: dict[str, int] = {}
    identities: dict[str, dict[str, int | str]] = {}
    for raw_skill in skills:
        if not isinstance(raw_skill, Mapping):
            raise ApiResponseError("avatar skill entry was invalid")
        group_id = positive_id(raw_skill.get("group_id"), label="skill group ID")
        field = suffixes.get(int(group_id) % 100)
        if field is None:
            continue
        if field in levels:
            raise ApiResponseError("core skill identity was duplicated")
        skill_id = positive_id(raw_skill.get("id"), label="skill ID")
        current_level = nonnegative_int(
            raw_skill.get("level_current"), label="core skill current level"
        )
        max_level = nonnegative_int(raw_skill.get("max_level"), label="core skill max level")
        if max_level <= 1 or current_level > max_level:
            raise ApiResponseError("core skill level contract was invalid")
        levels[field] = current_level
        identities[field] = {
            "skill_id": skill_id,
            "group_id": group_id,
            "max_level": max_level,
        }
    if set(levels) != set(CORE_SKILL_FIELDS):
        raise ApiResponseError("core skill triple was incomplete")
    if len({value["skill_id"] for value in identities.values()}) != len(CORE_SKILL_FIELDS):
        raise ApiResponseError("core skill IDs were not unique")
    if len({value["group_id"] for value in identities.values()}) != len(CORE_SKILL_FIELDS):
        raise ApiResponseError("core skill group IDs were not unique")
    return (
        {field: levels[field] for field in CORE_SKILL_FIELDS},
        {field: identities[field] for field in CORE_SKILL_FIELDS},
    )


def _source_semantic_digest(character_semantics: Mapping[str, Mapping[str, Any]]) -> str:
    encoded = json.dumps(
        {"characters": character_semantics},
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_snapshot(
    binding: Mapping[str, Any],
    avatars: list[dict[str, Any]],
    *,
    synced_at: datetime | None = None,
) -> Snapshot:
    timestamp = (synced_at or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    profile = {
        "schema_version": "game-profile/v1",
        "game": "genshin-impact",
        "account": dict(binding),
        "sync": {
            "state": "partial",
            "source": "miyoushe-web",
            "synced_at": timestamp.isoformat().replace("+00:00", "Z"),
        },
    }
    roster_items: dict[str, dict[str, Any]] = {}
    builds: dict[str, dict[str, Any]] = {}
    character_semantics: dict[str, dict[str, Any]] = {}
    weapon_count = 0
    for raw in avatars:
        identifier = positive_id(raw.get("id"), label="character ID")
        rarity = nonnegative_int(raw.get("avatar_level"), label="character rarity")
        friendship_level = bounded_int(
            raw.get("fetter_level"),
            label="friendship level",
            minimum=0,
            maximum=10,
        )
        roster_item = {
            "name": nonempty_string(raw.get("name"), label="character name"),
            "level": nonnegative_int(raw.get("level_current"), label="character level"),
            "ascension": nonnegative_int(raw.get("promote_level"), label="ascension"),
            "constellation": nonnegative_int(raw.get("constellation_num"), label="constellation"),
            "friendship_level": friendship_level,
        }
        semantic_item: dict[str, Any] = {"rarity": rarity}
        if identifier in TRAVELER_IDS:
            current_element, source_element_id = _traveler_element(raw)
            roster_item["current_element"] = current_element
            semantic_item["traveler_element"] = {
                "element_attr_id": source_element_id,
                "current_element": current_element,
            }
        roster_items[identifier] = roster_item
        progression, core_skill_identities = _progression(raw, character_id=identifier)
        semantic_item["core_skills"] = core_skill_identities
        character_semantics[identifier] = semantic_item
        build: dict[str, Any] = {
            "schema_version": "genshin-impact-build/v1",
            "game": "genshin-impact",
            "progression": progression,
        }
        weapon = _weapon(raw)
        build["weapon"] = weapon
        if weapon is not None:
            weapon_count += 1
        builds[identifier] = build
    traveler_ids = TRAVELER_IDS.intersection(roster_items)
    if len(traveler_ids) != 1:
        raise RosterCompletenessError("owned roster did not contain exactly one traveler")
    ordered_ids = sorted(roster_items)
    return Snapshot(
        profile=profile,
        roster={
            "schema_version": "genshin-impact-roster/v1",
            "game": "genshin-impact",
            "characters": {identifier: roster_items[identifier] for identifier in ordered_ids},
        },
        builds={identifier: builds[identifier] for identifier in ordered_ids},
        weapon_count=weapon_count,
        source_semantic_digest=_source_semantic_digest(character_semantics),
    )
