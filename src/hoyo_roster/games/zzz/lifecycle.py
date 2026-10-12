"""Long-term partial/complete-to-complete update rules and no-op detection."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .candidate import ZZZ_RELATIVE
from .errors import (
    AccountIdentityMismatch,
    CandidateValidationError,
    LifecycleRegressionError,
    SyncedAtRegressionError,
)
from .ids import parse_character_id
from .jsonio import (
    StrictJsonError,
    load_json,
    require_real_path_chain,
)
from .normalize import NormalizedSnapshot
from .snapshot import business_digest_from_files, canonical_business_digest

_UTC_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_CORE_RANK = {rank: index for index, rank in enumerate(("0", "A", "B", "C", "D", "E", "F"))}
_ROSTER_MONOTONIC_FIELDS = ("level", "promotion", "mindscape")
_SKILL_MONOTONIC_FIELDS = (
    "basic_attack_level",
    "dodge_level",
    "assist_level",
    "special_attack_level",
    "chain_attack_level",
)


@dataclass(frozen=True, slots=True)
class LifecycleDecision:
    no_op: bool
    old_business_digest: str | None
    new_business_digest: str


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CandidateValidationError(f"{label} was not an object")
    return value


def _utc_timestamp(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or _UTC_RFC3339.fullmatch(value) is None:
        raise CandidateValidationError(f"{label} was not UTC RFC 3339")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise CandidateValidationError(f"{label} was not UTC RFC 3339") from error
    if parsed.tzinfo != UTC:
        raise CandidateValidationError(f"{label} was not UTC RFC 3339")
    return parsed


def _formal_build_path(
    repo_root: Path,
    builds_dir: Path,
    character_id: object,
) -> tuple[str, Path]:
    normalized_id = parse_character_id(
        character_id,
        error_type=CandidateValidationError,
    )
    try:
        resolved_build = require_real_path_chain(
            repo_root,
            ZZZ_RELATIVE / "builds" / f"{normalized_id}.json",
            final_kind="regular_file",
        )
    except StrictJsonError as error:
        raise CandidateValidationError("formal Build path or regular file was invalid") from error
    if resolved_build.parent != builds_dir:
        raise CandidateValidationError("formal Build path escaped the Builds directory")
    return normalized_id, resolved_build


def _load_formal_snapshot(
    repo_root: Path,
) -> tuple[
    Mapping[str, Any],
    Mapping[str, Any],
    dict[str, Mapping[str, Any]],
    Mapping[str, Any],
]:
    try:
        data_dir = require_real_path_chain(
            repo_root,
            Path("data"),
            final_kind="directory",
        )
        root = require_real_path_chain(
            repo_root,
            ZZZ_RELATIVE,
            final_kind="directory",
        )
        builds_dir = require_real_path_chain(
            repo_root,
            ZZZ_RELATIVE / "builds",
            final_kind="directory",
        )
        profile_path = require_real_path_chain(
            repo_root,
            ZZZ_RELATIVE / "profile.json",
            final_kind="regular_file",
        )
        roster_path = require_real_path_chain(
            repo_root,
            ZZZ_RELATIVE / "roster.json",
            final_kind="regular_file",
        )
        bangboos_path = require_real_path_chain(
            repo_root,
            ZZZ_RELATIVE / "bangboos.json",
            final_kind="regular_file",
        )
        if root.parent != data_dir or builds_dir.parent != root:
            raise StrictJsonError("formal data directory containment was invalid")
        if any(path.parent != root for path in (profile_path, roster_path, bangboos_path)):
            raise StrictJsonError("formal data file containment was invalid")
        profile = _mapping(load_json(profile_path), label="formal profile")
        roster = _mapping(load_json(roster_path), label="formal roster")
        bangboos = _mapping(load_json(bangboos_path), label="formal Bangboos")
        characters = _mapping(roster.get("characters"), label="formal roster characters")
    except (OSError, StrictJsonError) as error:
        raise CandidateValidationError("formal ZZZ data was not strict JSON") from error
    builds: dict[str, Mapping[str, Any]] = {}
    for character_id in characters:
        normalized_id, build_path = _formal_build_path(
            repo_root,
            builds_dir,
            character_id,
        )
        try:
            builds[normalized_id] = _mapping(
                load_json(build_path),
                label="formal Build",
            )
        except (OSError, StrictJsonError) as error:
            raise CandidateValidationError("formal Build was not strict JSON") from error
    return profile, roster, builds, bangboos


def _add_regression(
    regressions: dict[str, set[str]],
    category: str,
    entity_id: str,
) -> None:
    regressions.setdefault(category, set()).add(entity_id)


def _check_character_progression(
    old_roster: Mapping[str, Any],
    new_roster: Mapping[str, Any],
    old_builds: Mapping[str, Mapping[str, Any]],
    new_builds: Mapping[str, Mapping[str, Any]],
    regressions: dict[str, set[str]],
) -> None:
    for character_id in set(old_roster) & set(new_roster):
        old_character = _mapping(old_roster[character_id], label="old roster character")
        new_character = _mapping(new_roster[character_id], label="new roster character")
        if any(new_character[field] < old_character[field] for field in _ROSTER_MONOTONIC_FIELDS):
            _add_regression(regressions, "character_progression", character_id)

        old_progression_value = old_builds[character_id].get("progression")
        if old_progression_value is None:
            continue
        old_progression = _mapping(
            old_progression_value,
            label="old character progression",
        )
        new_progression = _mapping(
            new_builds[character_id].get("progression"),
            label="new character progression",
        )
        if any(
            new_progression[field] < old_progression[field]
            for field in _SKILL_MONOTONIC_FIELDS
            if field in old_progression
        ):
            _add_regression(regressions, "character_progression", character_id)
        if "core_skill_rank" in old_progression:
            old_core = old_progression["core_skill_rank"]
            new_core = new_progression.get("core_skill_rank")
            if old_core not in _CORE_RANK or new_core not in _CORE_RANK:
                raise CandidateValidationError("core skill rank was outside the contract")
            if _CORE_RANK[str(new_core)] < _CORE_RANK[str(old_core)]:
                _add_regression(regressions, "character_progression", character_id)

        if "potential_vision" in old_progression:
            old_potential = old_progression["potential_vision"]
            new_potential = new_progression.get("potential_vision")
            if old_potential is not None and new_potential is None:
                _add_regression(regressions, "potential_vision", character_id)
            elif old_potential is not None and new_potential is not None:
                old_value = _mapping(old_potential, label="old Potential Vision")
                new_value = _mapping(new_potential, label="new Potential Vision")
                if any(
                    new_value[field] < old_value[field]
                    for field in ("level", "max_level")
                    if field in old_value
                ):
                    _add_regression(regressions, "potential_vision", character_id)


def _check_bangboos(
    old_bangboos: Mapping[str, Any],
    new_bangboos: Mapping[str, Any],
    regressions: dict[str, set[str]],
) -> None:
    for bangboo_id in set(old_bangboos) & set(new_bangboos):
        old_item = _mapping(old_bangboos[bangboo_id], label="old Bangboo")
        new_item = _mapping(new_bangboos[bangboo_id], label="new Bangboo")
        if new_item["level"] < old_item["level"] or new_item["core_level"] < old_item["core_level"]:
            _add_regression(regressions, "bangboo_progression", bangboo_id)


def evaluate_lifecycle(repo_root: Path, snapshot: NormalizedSnapshot) -> LifecycleDecision:
    """Validate identity, monotonic progression, time, and canonical no-op semantics."""

    profile, roster, old_builds, bangboos = _load_formal_snapshot(repo_root)
    new_digest = canonical_business_digest(snapshot)
    sync = _mapping(profile.get("sync"), label="formal sync")
    state = sync.get("state")
    if state == "not_synced":
        return LifecycleDecision(
            no_op=False,
            old_business_digest=None,
            new_business_digest=new_digest,
        )
    if state not in {"partial", "complete"}:
        raise CandidateValidationError("formal sync state was outside the contract")

    old_account = _mapping(profile.get("account"), label="formal account")
    new_account = _mapping(snapshot.profile.get("account"), label="new account")
    if old_account.get("uid") != new_account.get("uid") or old_account.get(
        "server"
    ) != new_account.get("server"):
        raise AccountIdentityMismatch("account identity mismatch category count: account:1")

    old_synced_at = _utc_timestamp(sync.get("synced_at"), label="old synced_at")
    new_sync = _mapping(snapshot.profile.get("sync"), label="new sync")
    new_synced_at = _utc_timestamp(new_sync.get("synced_at"), label="new synced_at")
    if new_synced_at <= old_synced_at:
        raise SyncedAtRegressionError("synced_at regression category count: synced_at:1")

    regressions: dict[str, set[str]] = {}
    if new_account["player_level"] < old_account["player_level"]:
        _add_regression(regressions, "player_level", "account")

    old_roster = _mapping(roster.get("characters"), label="formal roster characters")
    new_roster = _mapping(
        snapshot.roster.get("characters"),
        label="new roster characters",
    )
    missing_characters = set(old_roster) - set(new_roster)
    if missing_characters:
        regressions["roster_shrink"] = missing_characters

    old_bangboo_items = _mapping(bangboos.get("items"), label="formal Bangboos")
    new_bangboo_items = _mapping(snapshot.bangboos.get("items"), label="new Bangboos")
    missing_bangboos = set(old_bangboo_items) - set(new_bangboo_items)
    if missing_bangboos:
        regressions["bangboo_shrink"] = missing_bangboos

    _check_character_progression(
        old_roster,
        new_roster,
        old_builds,
        snapshot.builds,
        regressions,
    )
    _check_bangboos(old_bangboo_items, new_bangboo_items, regressions)
    if regressions:
        summary = ",".join(
            f"{category}:{len(regressions[category])}" for category in sorted(regressions)
        )
        raise LifecycleRegressionError(
            f"lifecycle regression categories and affected counts: {summary}"
        )

    old_digest = business_digest_from_files(
        profile=profile,
        roster=roster,
        builds=old_builds,
        bangboos=bangboos,
    )
    return LifecycleDecision(
        no_op=old_digest == new_digest,
        old_business_digest=old_digest,
        new_business_digest=new_digest,
    )
