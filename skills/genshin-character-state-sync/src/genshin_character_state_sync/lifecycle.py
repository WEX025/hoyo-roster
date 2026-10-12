"""Genshin partial snapshot lifecycle and coverage monotonicity."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import (
    CandidateValidationError,
    CompleteToPartialForbidden,
    LifecycleRegression,
    PartialFieldCoverageRegression,
)
from .jsonio import StrictJsonError, load_json, require_real_directory, require_regular_file
from .model import Snapshot
from .normalize import CORE_SKILL_FIELDS, ELEMENT_ATTR_TO_CANONICAL, TRAVELER_IDS
from .snapshot import business_digest

GENSHIN_RELATIVE = Path("data/genshin-impact")
_BUSINESS_FIELDS = ("progression", "weapon", "artifacts", "panel")
_ROSTER_MONOTONIC = ("level", "ascension", "constellation")


@dataclass(frozen=True, slots=True)
class LifecycleDecision:
    no_op: bool
    old_digest: str | None
    new_digest: str


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CandidateValidationError(f"{label} was invalid")
    return value


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CandidateValidationError("synced_at was invalid")
    try:
        result = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise CandidateValidationError("synced_at was invalid") from error
    if result.tzinfo != UTC:
        raise CandidateValidationError("synced_at was invalid")
    return result


def load_formal(
    repo_root: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]]:
    game = repo_root / GENSHIN_RELATIVE
    try:
        require_real_directory(game)
        builds_dir = game / "builds"
        require_real_directory(builds_dir)
        profile = load_json(game / "profile.json")
        roster = load_json(game / "roster.json")
        characters = _mapping(roster.get("characters"), label="formal roster")
        builds: dict[str, dict[str, Any]] = {}
        for character_id in characters:
            path = builds_dir / f"{character_id}.json"
            require_regular_file(path)
            value = load_json(path)
            if not isinstance(value, dict):
                raise CandidateValidationError("formal Build was invalid")
            builds[str(character_id)] = value
    except (OSError, StrictJsonError, AttributeError) as error:
        raise CandidateValidationError("formal Genshin data was unsafe") from error
    if not isinstance(profile, dict) or not isinstance(roster, dict):
        raise CandidateValidationError("formal Genshin snapshot was invalid")
    return profile, roster, builds


def _digest_from_files(
    profile: dict[str, Any], roster: dict[str, Any], builds: dict[str, dict[str, Any]]
) -> str:
    return business_digest(Snapshot(profile, roster, builds, 0))


def _field_set(value: object) -> set[str]:
    return set(value) if isinstance(value, Mapping) else set()


def _coverage_regresses(old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
    for field in _BUSINESS_FIELDS:
        if field not in old:
            continue
        if field not in new:
            return True
        old_value = old[field]
        new_value = new[field]
        if isinstance(old_value, Mapping):
            if field == "weapon" and new_value is None:
                continue
            if not isinstance(new_value, Mapping) or not _field_set(old_value) <= _field_set(
                new_value
            ):
                return True
            if field == "artifacts":
                for slot in old_value:
                    old_slot = old_value[slot]
                    new_slot = new_value[slot]
                    if (
                        isinstance(old_slot, Mapping)
                        and isinstance(new_slot, Mapping)
                        and not _field_set(old_slot) <= _field_set(new_slot)
                    ):
                        return True
        elif field not in {"weapon"} and old_value is not None and new_value is None:
            return True
    return False


def _progression_decreased(old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
    old_progression = old.get("progression")
    if not isinstance(old_progression, Mapping):
        return False
    new_progression = new.get("progression")
    if not isinstance(new_progression, Mapping):
        return False
    return any(
        field in old_progression
        and field in new_progression
        and new_progression[field] < old_progression[field]
        for field in CORE_SKILL_FIELDS
    )


def evaluate_lifecycle(repo_root: Path, snapshot: Snapshot) -> LifecycleDecision:
    profile, roster, old_builds = load_formal(repo_root)
    new_digest = business_digest(snapshot)
    sync = _mapping(profile.get("sync"), label="formal sync")
    state = sync.get("state")
    if state == "not_synced":
        return LifecycleDecision(False, None, new_digest)
    if state == "complete":
        raise CompleteToPartialForbidden("complete snapshot cannot be replaced by partial")
    if state != "partial":
        raise CandidateValidationError("formal sync state was invalid")

    old_account = _mapping(profile.get("account"), label="formal account")
    new_account = _mapping(snapshot.profile.get("account"), label="candidate account")
    regressions: dict[str, int] = {}
    if old_account.get("uid") != new_account.get("uid"):
        regressions["uid"] = 1
    if old_account.get("server") != new_account.get("server"):
        regressions["server"] = 1
    if new_account.get("player_level", -1) < old_account.get("player_level", 0):
        regressions["player_level"] = 1
    old_characters = _mapping(roster.get("characters"), label="formal roster")
    new_characters = _mapping(snapshot.roster.get("characters"), label="candidate roster")
    new_travelers = TRAVELER_IDS & new_characters.keys()
    if len(new_travelers) != 1:
        raise CandidateValidationError("candidate roster did not contain exactly one traveler")
    missing = set(old_characters) - set(new_characters)
    if missing:
        regressions["roster_shrink"] = len(missing)
    progression_count = 0
    friendship_count = 0
    coverage_count = 0
    roster_coverage_count = 0
    progression_details: set[str] = set()
    for character_id in set(old_characters) & set(new_characters):
        old_character = _mapping(old_characters[character_id], label="formal character")
        new_character = _mapping(new_characters[character_id], label="candidate character")
        old_element = old_character.get("current_element")
        new_element = new_character.get("current_element")
        confirmed_element_switch = (
            character_id in TRAVELER_IDS
            and old_element in ELEMENT_ATTR_TO_CANONICAL.values()
            and new_element in ELEMENT_ATTR_TO_CANONICAL.values()
            and old_element != new_element
        )
        monotonic_fields = tuple(
            field
            for field in _ROSTER_MONOTONIC
            if field != "constellation" or not confirmed_element_switch
        )
        if any(new_character[field] < old_character[field] for field in monotonic_fields):
            progression_count += 1
            progression_details.update(
                f"character_{field}"
                for field in monotonic_fields
                if new_character[field] < old_character[field]
            )
        if "friendship_level" in old_character:
            if "friendship_level" not in new_character:
                roster_coverage_count += 1
            elif new_character["friendship_level"] < old_character["friendship_level"]:
                friendship_count += 1
        if character_id not in TRAVELER_IDS:
            if "current_element" in new_character:
                raise CandidateValidationError("non-traveler contained current_element")
        elif "current_element" in old_character and "current_element" not in new_character:
            roster_coverage_count += 1
        old_build = old_builds[character_id]
        new_build = snapshot.builds[character_id]
        if _coverage_regresses(old_build, new_build):
            coverage_count += 1
        element_unchanged = character_id not in TRAVELER_IDS or (
            old_element is not None and old_element == new_element
        )
        if element_unchanged and _progression_decreased(old_build, new_build):
            progression_count += 1
            progression_details.update(
                f"skill_{field}"
                for field in CORE_SKILL_FIELDS
                if field in old_build["progression"]
                and field in new_build["progression"]
                and new_build["progression"][field] < old_build["progression"][field]
            )
    if coverage_count or roster_coverage_count:
        raise PartialFieldCoverageRegression(
            "partial field coverage regression affected entities: "
            f"{coverage_count + roster_coverage_count}"
        )
    if friendship_count:
        regressions["friendship_level"] = friendship_count
    if progression_count:
        regressions["character_progression"] = progression_count
    if regressions:
        summary = ",".join(f"{key}:{regressions[key]}" for key in sorted(regressions))
        raise LifecycleRegression(
            f"lifecycle regression categories and counts: {summary}",
            categories=tuple(sorted(regressions)),
            details=tuple(sorted(progression_details)),
        )
    if _timestamp(snapshot.profile["sync"]["synced_at"]) <= _timestamp(sync.get("synced_at")):
        raise LifecycleRegression(
            "lifecycle regression categories and counts: synced_at:1", categories=("synced_at",)
        )
    old_digest = _digest_from_files(profile, roster, old_builds)
    return LifecycleDecision(old_digest == new_digest, old_digest, new_digest)
