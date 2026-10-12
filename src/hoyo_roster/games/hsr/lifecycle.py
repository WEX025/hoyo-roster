"""HSR partial snapshot lifecycle and coverage monotonicity."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, replace
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
from .model import PassObservation, RosterCoverageStatus, Snapshot, SnapshotDiagnostics
from .normalize import normalize_profile
from .snapshot import business_digest

HSR_RELATIVE = Path("data/honkai-star-rail")
_PROGRESSION_LEVELS = (
    "basic_level",
    "skill_level",
    "ultimate_level",
    "talent_level",
)
_EMPTY_DIAGNOSTICS = SnapshotDiagnostics(0, 0, 0, 0, 0, 0, 0, (), (), 0)


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
    game = repo_root / HSR_RELATIVE
    try:
        require_real_directory(game)
        builds_dir = game / "builds"
        require_real_directory(builds_dir)
        profile = load_json(game / "profile.json")
        roster = load_json(game / "roster.json")
        if not isinstance(profile, dict) or not isinstance(roster, dict):
            raise CandidateValidationError("formal HSR snapshot was invalid")
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
        raise CandidateValidationError("formal HSR data was unsafe") from error
    return profile, roster, builds


def snapshot_from_files(
    profile: dict[str, Any],
    roster: dict[str, Any],
    builds: dict[str, dict[str, Any]],
) -> Snapshot:
    return Snapshot(profile, roster, builds, _EMPTY_DIAGNOSTICS, {})


def materialize_snapshot(
    formal_profile: dict[str, Any],
    formal_roster: dict[str, Any],
    formal_builds: dict[str, dict[str, Any]],
    observation: PassObservation,
    *,
    synced_at: datetime,
) -> Snapshot:
    sync = _mapping(formal_profile.get("sync"), label="formal sync")
    state = sync.get("state")
    if state == "complete":
        raise CompleteToPartialForbidden("complete snapshot cannot be replaced by partial")
    if state not in {"not_synced", "partial"}:
        raise CandidateValidationError("formal sync state was invalid")
    profile = normalize_profile(observation.binding, synced_at=synced_at)
    coverage = observation.coverage_document()
    if observation.roster_coverage is RosterCoverageStatus.OBSERVED_NONEMPTY:
        if observation.snapshot is None:
            raise CandidateValidationError("observed Snapshot was unavailable")
        return replace(
            observation.snapshot,
            profile=profile,
            source_coverage={
                **observation.snapshot.source_coverage,
                **coverage,
            },
        )
    return Snapshot(
        profile=profile,
        roster=copy.deepcopy(formal_roster),
        builds=copy.deepcopy(formal_builds),
        diagnostics=_EMPTY_DIAGNOSTICS,
        source_coverage=coverage,
    )


def _coverage_regresses(old: object, new: object) -> bool:
    if not isinstance(old, Mapping):
        return False
    if not isinstance(new, Mapping):
        return True
    if not set(old) <= set(new):
        return True
    return any(
        _coverage_regresses(old[key], new[key]) for key in old if isinstance(old[key], Mapping)
    )


def _current_coverage_regresses(
    old_build: Mapping[str, Any],
    new_build: Mapping[str, Any],
) -> bool:
    for field in ("progression", "light_cone", "relics", "panel", "panel_context"):
        if field not in old_build:
            continue
        if field not in new_build:
            return True
        old = old_build[field]
        new = new_build[field]
        if field == "light_cone" and isinstance(old, Mapping) and new is None:
            continue
        if field == "relics" and isinstance(old, Mapping) and isinstance(new, Mapping):
            if not set(old) <= set(new):
                return True
            for slot in old:
                old_item = old[slot]
                new_item = new[slot]
                if isinstance(old_item, Mapping) and new_item is None:
                    continue
                if _coverage_regresses(old_item, new_item):
                    return True
            continue
        if _coverage_regresses(old, new):
            return True
    return False


def _level_decreased(old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
    old_progression = old.get("progression")
    if not isinstance(old_progression, Mapping):
        return False
    new_progression = new.get("progression")
    if not isinstance(new_progression, Mapping):
        return True
    for field in _PROGRESSION_LEVELS:
        if field in old_progression and new_progression.get(field, -1) < old_progression[field]:
            return True
    old_nodes = old_progression.get("trace_nodes")
    new_nodes = new_progression.get("trace_nodes")
    if isinstance(old_nodes, Mapping):
        if not isinstance(new_nodes, Mapping):
            return True
        for point_id, old_node in old_nodes.items():
            if point_id not in new_nodes:
                return True
            if isinstance(old_node, Mapping) and "level" in old_node:
                new_node = new_nodes[point_id]
                if (
                    not isinstance(new_node, Mapping)
                    or new_node.get("level", -1) < old_node["level"]
                ):
                    return True
    old_memosprite = old_progression.get("memosprite")
    new_memosprite = new_progression.get("memosprite")
    if isinstance(old_memosprite, Mapping):
        if not isinstance(new_memosprite, Mapping):
            return True
        for category in ("skills", "talents"):
            old_abilities = old_memosprite.get(category)
            new_abilities = new_memosprite.get(category)
            if not isinstance(old_abilities, Mapping):
                continue
            if not isinstance(new_abilities, Mapping):
                return True
            for ability_id, old_ability in old_abilities.items():
                new_ability = new_abilities.get(ability_id)
                if (
                    isinstance(old_ability, Mapping)
                    and "level" in old_ability
                    and (
                        not isinstance(new_ability, Mapping)
                        or new_ability.get("level", -1) < old_ability["level"]
                    )
                ):
                    return True
    return False


def evaluate_lifecycle(repo_root: Path, snapshot: Snapshot) -> LifecycleDecision:
    profile, roster, old_builds = load_formal(repo_root)
    new_digest = business_digest(snapshot)
    if snapshot.profile.get("sync", {}).get("state") != "partial":
        raise CandidateValidationError("candidate sync state was not partial")
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
    missing = set(old_characters) - set(new_characters)
    if missing:
        regressions["roster_shrink"] = len(missing)
    roster_coverage = 0
    character_level = 0
    build_coverage = 0
    permanent_level = 0
    for character_id in set(old_characters) & set(new_characters):
        old_character = _mapping(old_characters[character_id], label="formal character")
        new_character = _mapping(new_characters[character_id], label="candidate character")
        if not set(old_character) <= set(new_character):
            roster_coverage += 1
        if new_character.get("level", -1) < old_character.get("level", 0):
            character_level += 1
        old_build = _mapping(old_builds[character_id], label="formal Build")
        new_build = _mapping(snapshot.builds[character_id], label="candidate Build")
        if _current_coverage_regresses(old_build, new_build):
            build_coverage += 1
        if _level_decreased(old_build, new_build):
            permanent_level += 1
    if roster_coverage or build_coverage:
        raise PartialFieldCoverageRegression(
            "partial field coverage regression affected entities: "
            f"{roster_coverage + build_coverage}"
        )
    if character_level:
        regressions["character_level"] = character_level
    if permanent_level:
        regressions["permanent_progression"] = permanent_level
    if regressions:
        summary = ",".join(f"{key}:{regressions[key]}" for key in sorted(regressions))
        raise LifecycleRegression(f"lifecycle regression categories and counts: {summary}")

    if _timestamp(snapshot.profile["sync"]["synced_at"]) <= _timestamp(sync.get("synced_at")):
        raise LifecycleRegression("lifecycle regression categories and counts: synced_at:1")
    old_digest = business_digest(snapshot_from_files(profile, roster, old_builds))
    return LifecycleDecision(old_digest == new_digest, old_digest, new_digest)
