"""Strict source-to-repository normalization by stable IDs."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from .endpoints import CollectedState, CompletenessEvidence, cultivate_owned_ids
from .errors import MappingError, SnapshotConsistencyError
from .ids import (
    parse_bangboo_id,
    parse_character_id,
    parse_drive_disc_set_id,
    parse_w_engine_id,
)

SKILL_TYPE_FIELDS = {
    0: "basic_attack_level",
    2: "dodge_level",
    6: "assist_level",
    1: "special_attack_level",
    3: "chain_attack_level",
    5: "core_skill_rank",
}
CORE_SKILL_LEVEL_TO_RANK = {
    1: "0",
    2: "A",
    3: "B",
    4: "C",
    5: "D",
    6: "E",
    7: "F",
}
PANEL_PROPERTY_FIELDS = {
    1: "hp",
    2: "attack",
    3: "defense",
    4: "impact",
    5: "crit_rate",
    6: "crit_damage",
    7: "anomaly_mastery",
    8: "anomaly_proficiency",
    9: "penetration_ratio",
    11: "energy_regeneration",
    19: "sheer_force",
    20: "flash_energy_accumulation",
    232: "penetration",
    315: "physical_damage_bonus",
    316: "fire_damage_bonus",
    317: "ice_damage_bonus",
    318: "electric_damage_bonus",
    319: "ether_damage_bonus",
    323: "wind_damage_bonus",
}
REQUIRED_PANEL_PROPERTY_IDS = frozenset({1, 2, 3, 4, 5, 6, 7, 8})
ZERO_WHEN_ABSENT_PANEL_PROPERTY_IDS = frozenset({9, 11, 232})
CORE_PANEL_PROPERTY_IDS_IN_OUTPUT_ORDER = (
    1,
    2,
    3,
    4,
    5,
    6,
    8,
    7,
    232,
    9,
    11,
)
OPTIONAL_PANEL_PROPERTY_IDS_IN_OUTPUT_ORDER = (
    19,
    20,
    315,
    316,
    317,
    318,
    319,
    323,
)
DISC_PROPERTY_KEYS = {
    11101: "hp",
    11102: "hp_percent",
    11103: "hp",
    12101: "attack",
    12102: "attack_percent",
    12103: "attack",
    13101: "defense",
    13102: "defense_percent",
    13103: "defense",
    12202: "impact",
    20103: "crit_rate",
    21103: "crit_damage",
    23103: "penetration_ratio",
    23203: "penetration",
    30502: "energy_regeneration",
    31203: "anomaly_proficiency",
    31402: "anomaly_mastery",
    31503: "physical_damage_bonus",
    31603: "fire_damage_bonus",
    31703: "ice_damage_bonus",
    31803: "electric_damage_bonus",
    31903: "ether_damage_bonus",
    32303: "wind_damage_bonus",
}
MAIN_PROPERTY_IDS_BY_SLOT = {
    1: frozenset({11103}),
    2: frozenset({12103}),
    3: frozenset({13103}),
    4: frozenset({11102, 12102, 13102, 20103, 21103, 31203}),
    5: frozenset(
        {
            11102,
            12102,
            13102,
            23103,
            31503,
            31603,
            31703,
            31803,
            31903,
            32303,
        }
    ),
    6: frozenset({11102, 12102, 13102, 12202, 31402, 30502}),
}
SUBSTAT_PROPERTY_IDS = frozenset(
    {11103, 11102, 12103, 12102, 13103, 13102, 20103, 21103, 31203, 23203}
)
RARITY_VALUES = {"B": 3, "A": 4, "S": 5}
IGNORED_PANEL_PROPERTY_REASONS: Mapping[int, str] = {}
CanonicalNumber = Decimal | int
_DECIMAL_HUNDRED = Decimal(100)
_DECIMAL_SOURCE = re.compile(
    r"[+-]?(?:(?:\d{1,3}(?:,\d{3})+)|\d+)(?:\.\d+)?\Z"
)
_NONFINITE_DECIMAL_SOURCES = frozenset(
    {
        "nan",
        "+nan",
        "-nan",
        "inf",
        "+inf",
        "-inf",
        "infinity",
        "+infinity",
        "-infinity",
    }
)


@dataclass
class NormalizationDiagnostics:
    ignored_panel_property_ids: set[int] = field(default_factory=set)


@dataclass(frozen=True)
class NormalizedSnapshot:
    profile: dict[str, Any]
    roster: dict[str, Any]
    builds: Mapping[str, dict[str, Any]]
    bangboos: dict[str, Any]
    diagnostics: NormalizationDiagnostics
    completeness: CompletenessEvidence

    @property
    def w_engine_count(self) -> int:
        return sum(build["w_engine"] is not None for build in self.builds.values())

    @property
    def drive_disc_count(self) -> int:
        return sum(
            disc is not None
            for build in self.builds.values()
            for disc in build["drive_discs"].values()
        )


def _object(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MappingError(f"{label}: expected an object")
    return value


def _list(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise MappingError(f"{label}: expected a list")
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise MappingError(f"{label}: expected an integer >= {minimum}")
    return value


def _name(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MappingError(f"{label}: name was missing")
    return value


def _parse_number(value: object, label: str) -> CanonicalNumber:
    if isinstance(value, bool):
        raise MappingError(f"{label}: boolean is not numeric")
    percentage = False
    if isinstance(value, str):
        normalized = value.strip()
        percentage = normalized.endswith("%")
        if percentage:
            normalized = normalized[:-1].strip()
        if normalized.casefold() in _NONFINITE_DECIMAL_SOURCES:
            raise MappingError(f"{label}: numeric value must be finite and nonnegative")
        if _DECIMAL_SOURCE.fullmatch(normalized) is None:
            raise MappingError(f"{label}: numeric value was invalid")
        try:
            parsed = Decimal(normalized.replace(",", ""))
        except InvalidOperation:
            raise MappingError(f"{label}: numeric value was invalid") from None
    elif isinstance(value, int):
        parsed = Decimal(value)
    elif isinstance(value, float):
        # repr() is Python's shortest round-trip decimal for the supplied float.
        parsed = Decimal(repr(value))
    elif isinstance(value, Decimal):
        parsed = value
    else:
        raise MappingError(f"{label}: numeric value was missing")
    if not parsed.is_finite() or parsed < 0:
        raise MappingError(f"{label}: numeric value must be finite and nonnegative")
    if percentage:
        with localcontext() as context:
            context.prec = max(context.prec, len(parsed.as_tuple().digits) + 2)
            parsed = parsed / _DECIMAL_HUNDRED
    return int(parsed) if parsed == parsed.to_integral_value() else parsed


def _rarity(value: object, label: str) -> int:
    if isinstance(value, str):
        if value not in RARITY_VALUES:
            raise MappingError(f"{label}: unknown rarity")
        return RARITY_VALUES[value]
    return _integer(value, label)


def _display_rank(value: object, label: str) -> int:
    rank = _integer(value, label, minimum=1)
    if rank > 5:
        raise MappingError(f"{label}: expected an integer in 1..5")
    return rank


def normalize_skills(detail: Mapping[str, Any]) -> dict[str, Any]:
    progression: dict[str, Any] = {}
    seen: set[int] = set()
    for raw_skill in _list(detail.get("skills"), "skills"):
        skill = _object(raw_skill, "skill")
        skill_type = _integer(skill.get("skill_type"), "skill_type")
        if skill_type not in SKILL_TYPE_FIELDS:
            raise MappingError("unknown skill_type")
        if skill_type in seen:
            raise MappingError("duplicate skill_type")
        seen.add(skill_type)
        level = _integer(skill.get("level"), "skill level")
        field_name = SKILL_TYPE_FIELDS[skill_type]
        if skill_type == 5:
            if level not in CORE_SKILL_LEVEL_TO_RANK:
                raise MappingError("core skill level must be an integer in 1..7")
            progression[field_name] = CORE_SKILL_LEVEL_TO_RANK[level]
        else:
            progression[field_name] = level
    if seen != set(SKILL_TYPE_FIELDS):
        raise MappingError("skill_type coverage was incomplete")
    progression["potential_vision"] = normalize_potential(detail.get("skill_awaken"))
    return progression


def normalize_potential(value: object) -> dict[str, int] | None:
    if value is None:
        return None
    potential = _object(value, "skill_awaken")
    has_system = potential.get("has_awaken_system")
    if has_system is False:
        return None
    if has_system is not True:
        raise MappingError("skill_awaken availability was invalid")
    level = _integer(potential.get("awaken_level"), "potential level")
    max_level = _integer(potential.get("awaken_max_level"), "potential max_level")
    if level > max_level:
        raise MappingError("potential level exceeds max_level")
    return {"level": level, "max_level": max_level}


def normalize_panel(
    detail: Mapping[str, Any],
    diagnostics: NormalizationDiagnostics,
) -> dict[str, CanonicalNumber | None]:
    values: dict[int, CanonicalNumber] = {}
    for raw_property in _list(detail.get("properties"), "panel properties"):
        prop = _object(raw_property, "panel property")
        property_id = _integer(prop.get("property_id"), "panel property_id")
        field_name = PANEL_PROPERTY_FIELDS.get(property_id)
        if field_name is None:
            if property_id in IGNORED_PANEL_PROPERTY_REASONS:
                diagnostics.ignored_panel_property_ids.add(property_id)
                continue
            raise MappingError("unknown panel property_id")
        if property_id in values:
            raise MappingError("duplicate panel property_id")
        values[property_id] = _parse_number(prop.get("final"), f"panel property {property_id}")

    missing_required = sorted(REQUIRED_PANEL_PROPERTY_IDS - values.keys())
    if missing_required:
        missing = ",".join(str(property_id) for property_id in missing_required)
        raise MappingError(f"required panel property_id missing: {missing}")

    panel: dict[str, CanonicalNumber | None] = {}
    for property_id in CORE_PANEL_PROPERTY_IDS_IN_OUTPUT_ORDER:
        field_name = PANEL_PROPERTY_FIELDS[property_id]
        if property_id in ZERO_WHEN_ABSENT_PANEL_PROPERTY_IDS:
            panel[field_name] = values.get(property_id, 0)
        else:
            panel[field_name] = values[property_id]
    for property_id in OPTIONAL_PANEL_PROPERTY_IDS_IN_OUTPUT_ORDER:
        panel[PANEL_PROPERTY_FIELDS[property_id]] = values.get(property_id)
    return panel


def normalize_w_engine(value: object) -> dict[str, Any] | None:
    if value is None or value == {}:
        return None
    weapon = _object(value, "weapon")
    if weapon.get("id") == 0:
        return None
    return {
        "w_engine_id": parse_w_engine_id(weapon.get("id")),
        "name": _name(weapon.get("name"), "weapon"),
        "rarity": _rarity(weapon.get("rarity"), "weapon rarity"),
        "level": _integer(weapon.get("level"), "weapon level"),
        "modification": _display_rank(weapon.get("star"), "weapon star"),
    }


def _disc_property(value: object, label: str) -> tuple[int, str, CanonicalNumber]:
    prop = _object(value, label)
    property_id = _integer(prop.get("property_id"), f"{label} property_id")
    key = DISC_PROPERTY_KEYS.get(property_id)
    if key is None:
        raise MappingError(f"{label}: unknown equipped property_id")
    return property_id, key, _parse_number(prop.get("base"), f"{label} value")


def normalize_drive_discs(value: object) -> dict[str, dict[str, Any] | None]:
    slots: dict[str, dict[str, Any] | None] = {f"slot_{slot}": None for slot in range(1, 7)}
    for raw_disc in _list(value, "equip"):
        disc = _object(raw_disc, "drive disc")
        slot = _integer(disc.get("equipment_type"), "equipment_type", minimum=1)
        if slot not in MAIN_PROPERTY_IDS_BY_SLOT:
            raise MappingError("illegal drive disc slot")
        slot_name = f"slot_{slot}"
        if slots[slot_name] is not None:
            raise MappingError("duplicate drive disc slot")
        main_properties = _list(disc.get("main_properties"), "drive disc main_properties")
        if len(main_properties) != 1:
            raise MappingError("drive disc must have exactly one main property")
        main_id, main_key, main_value = _disc_property(
            main_properties[0],
            "drive disc main property",
        )
        if main_id not in MAIN_PROPERTY_IDS_BY_SLOT[slot]:
            raise MappingError("drive disc main property_id is invalid for its slot")

        substats: list[dict[str, Any]] = []
        seen_substats: set[str] = set()
        for raw_substat in _list(disc.get("properties"), "drive disc properties"):
            property_id, key, property_value = _disc_property(raw_substat, "drive disc substat")
            if property_id not in SUBSTAT_PROPERTY_IDS:
                raise MappingError("drive disc property_id is not a valid substat")
            if key in seen_substats:
                raise MappingError("duplicate drive disc substat")
            seen_substats.add(key)
            substats.append({"key": key, "value": property_value})
        substats.sort(key=lambda item: item["key"])

        suit = _object(disc.get("equip_suit"), "drive disc suit")
        slots[slot_name] = {
            "set_id": parse_drive_disc_set_id(suit.get("suit_id")),
            "set_name": _name(
                suit.get("name", suit.get("name_mi18n")),
                "drive disc suit",
            ),
            "rarity": _rarity(disc.get("rarity"), "drive disc rarity"),
            "level": _integer(disc.get("level"), "drive disc level"),
            "main_stat": {"key": main_key, "value": main_value},
            "substats": substats,
        }
    return slots


def _normalize_bangboos(data: Mapping[str, Any]) -> dict[str, Any]:
    items: dict[str, dict[str, Any]] = {}
    for raw_item in _list(data.get("list"), "bangboo list"):
        item = _object(raw_item, "bangboo")
        bangboo_id = parse_bangboo_id(item.get("id"))
        if bangboo_id in items:
            raise SnapshotConsistencyError("bangboo list repeated an entity")
        items[bangboo_id] = {
            "name": _name(item.get("name"), "bangboo"),
            "rarity": _rarity(item.get("rarity"), "bangboo rarity"),
            "level": _integer(item.get("level"), "bangboo level"),
            "core_level": _display_rank(item.get("star"), "bangboo star"),
        }
    return {
        "schema_version": "zenless-zone-zero-bangboos/v1",
        "game": "zenless-zone-zero",
        "items": {key: items[key] for key in sorted(items)},
    }


def normalize_collection(
    collected: CollectedState,
    *,
    synced_at: datetime | None = None,
) -> NormalizedSnapshot:
    """Normalize a complete collection without recommendations or inventory data."""

    basic_items = _list(collected.basic.get("avatar_list"), "avatar/basic list")
    basic_by_id: dict[str, Mapping[str, Any]] = {}
    for raw_item in basic_items:
        item = _object(raw_item, "avatar/basic item")
        character_id = parse_character_id(item.get("id"))
        if character_id in basic_by_id:
            raise SnapshotConsistencyError("avatar/basic repeated a character")
        basic_by_id[character_id] = item
    if not basic_by_id:
        raise SnapshotConsistencyError("owned roster is empty")

    basic_ids = set(basic_by_id)
    if basic_ids != cultivate_owned_ids(collected.cultivate_basic):
        raise SnapshotConsistencyError(
            "avatar/basic and cultivate owned rosters were not exactly equal"
        )
    if set(collected.character_details) != basic_ids:
        raise SnapshotConsistencyError("character detail coverage is incomplete")
    if set(collected.cultivate_details) != basic_ids:
        raise SnapshotConsistencyError("cultivate detail coverage is incomplete")

    diagnostics = NormalizationDiagnostics()
    roster_characters: dict[str, dict[str, Any]] = {}
    builds: dict[str, dict[str, Any]] = {}
    for character_id in sorted(basic_ids):
        basic = basic_by_id[character_id]
        cultivate = _object(
            collected.cultivate_details[character_id].get("avatar"),
            "cultivate detail avatar",
        )
        roster_characters[character_id] = {
            "name": _name(basic.get("name_mi18n", basic.get("name")), "character"),
            "level": _integer(basic.get("level"), "character level"),
            "promotion": _integer(cultivate.get("promotes"), "character promotion"),
            "mindscape": _integer(basic.get("rank"), "character rank"),
        }
        detail = collected.character_details[character_id]
        builds[character_id] = {
            "schema_version": "zenless-zone-zero-build/v1",
            "game": "zenless-zone-zero",
            "panel_context": "out_of_combat",
            "progression": normalize_skills(detail),
            "w_engine": normalize_w_engine(detail.get("weapon")),
            "drive_discs": normalize_drive_discs(detail.get("equip")),
            "panel": normalize_panel(detail, diagnostics),
        }

    timestamp = synced_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        raise MappingError("synced_at must be timezone-aware")
    timestamp_text = timestamp.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    return NormalizedSnapshot(
        profile={
            "schema_version": "game-profile/v1",
            "game": "zenless-zone-zero",
            "account": {
                "uid": collected.role.uid,
                "server": collected.role.server,
                "nickname": collected.role.nickname,
                "player_level": collected.role.player_level,
            },
            "sync": {
                "state": "complete",
                "source": "miyoushe-web",
                "synced_at": timestamp_text,
            },
        },
        roster={
            "schema_version": "zenless-zone-zero-roster/v1",
            "game": "zenless-zone-zero",
            "characters": roster_characters,
        },
        builds=builds,
        bangboos=_normalize_bangboos(collected.bangboos),
        diagnostics=diagnostics,
        completeness=collected.completeness,
    )
