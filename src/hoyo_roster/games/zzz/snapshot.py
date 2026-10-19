"""Canonical business digests and privacy-safe two-pass drift diagnostics."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from .errors import SnapshotContentDrift
from hoyo_roster.jsonio import canonical_compact_json_bytes
from .normalize import NormalizedSnapshot


def canonical_business_document(snapshot: NormalizedSnapshot) -> dict[str, Any]:
    """Project only durable business state, excluding sync and run metadata."""

    return {
        "account": snapshot.profile["account"],
        "roster": snapshot.roster,
        "builds": {key: snapshot.builds[key] for key in sorted(snapshot.builds)},
        "bangboos": snapshot.bangboos,
    }


def canonical_business_document_from_files(
    *,
    profile: Mapping[str, Any],
    roster: Mapping[str, Any],
    builds: Mapping[str, Mapping[str, Any]],
    bangboos: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "account": profile["account"],
        "roster": roster,
        "builds": {key: builds[key] for key in sorted(builds)},
        "bangboos": bangboos,
    }


def _digest(document: Mapping[str, Any]) -> str:
    encoded = canonical_compact_json_bytes(
        document,
        sort_keys=True,
    )
    return hashlib.sha256(encoded).hexdigest()


def canonical_business_digest(snapshot: NormalizedSnapshot) -> str:
    return _digest(canonical_business_document(snapshot))


def business_digest_from_files(
    *,
    profile: Mapping[str, Any],
    roster: Mapping[str, Any],
    builds: Mapping[str, Mapping[str, Any]],
    bangboos: Mapping[str, Any],
) -> str:
    return _digest(
        canonical_business_document_from_files(
            profile=profile,
            roster=roster,
            builds=builds,
            bangboos=bangboos,
        )
    )


def _changed_mapping_count(left: Mapping[str, Any], right: Mapping[str, Any]) -> int:
    keys = set(left) | set(right)
    return sum(left.get(key) != right.get(key) for key in keys)


def _drift_summary(
    first: NormalizedSnapshot,
    second: NormalizedSnapshot,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    first_account = first.profile["account"]
    second_account = second.profile["account"]
    account_count = sum(
        first_account.get(key) != second_account.get(key)
        for key in ("uid", "server", "nickname", "player_level")
    )
    if account_count:
        counts["account"] = account_count

    first_roster = first.roster["characters"]
    second_roster = second.roster["characters"]
    roster_count = _changed_mapping_count(first_roster, second_roster)
    if roster_count:
        counts["roster"] = roster_count

    first_build_ids = set(first.builds)
    second_build_ids = set(second.builds)
    build_id_count = len(first_build_ids ^ second_build_ids)
    if build_id_count:
        counts["build_ids"] = build_id_count
    for category, field in (
        ("character_progression", "progression"),
        ("w_engine", "w_engine"),
        ("drive_discs", "drive_discs"),
        ("panel", "panel"),
    ):
        changed = sum(
            first.builds[character_id].get(field) != second.builds[character_id].get(field)
            for character_id in first_build_ids & second_build_ids
        )
        if changed:
            counts[category] = changed

    first_bangboos = first.bangboos["items"]
    second_bangboos = second.bangboos["items"]
    bangboo_count = _changed_mapping_count(first_bangboos, second_bangboos)
    if bangboo_count:
        counts["bangboos"] = bangboo_count
    return counts


def assert_stable_business_snapshots(
    first: NormalizedSnapshot,
    second: NormalizedSnapshot,
) -> tuple[str, str]:
    """Require exact canonical business equality for the fixed two-pass protocol."""

    first_digest = canonical_business_digest(first)
    second_digest = canonical_business_digest(second)
    if first_digest != second_digest:
        counts = _drift_summary(first, second)
        summary = ",".join(f"{category}:{counts[category]}" for category in sorted(counts))
        raise SnapshotContentDrift(
            f"snapshot content drift categories and counts: {summary or 'unknown:1'}"
        )
    return first_digest, second_digest
