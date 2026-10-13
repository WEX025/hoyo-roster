"""Canonical business digests and privacy-safe two-pass comparison."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from .errors import SnapshotContentDrift
from hoyo_roster.jsonio import json_bytes
from .model import Snapshot


def business_document(snapshot: Snapshot) -> dict[str, Any]:
    return {
        "account": snapshot.profile["account"],
        "roster": snapshot.roster,
        "builds": {key: snapshot.builds[key] for key in sorted(snapshot.builds)},
    }


def _digest(value: Any) -> str:
    return hashlib.sha256(json_bytes(value, sort_keys=True, compact=True)).hexdigest()


def business_digest(snapshot: Snapshot) -> str:
    return _digest(business_document(snapshot))


def source_coverage_digest(snapshot: Snapshot) -> str:
    return _digest(snapshot.source_coverage)


def roster_id_set_digest(snapshot: Snapshot) -> str:
    return _digest(sorted(snapshot.roster["characters"]))


def roster_business_digest(snapshot: Snapshot) -> str:
    return _digest(snapshot.roster)


def _changed(left: Mapping[str, Any], right: Mapping[str, Any]) -> int:
    return sum(left.get(key) != right.get(key) for key in set(left) | set(right))


def assert_stable(first: Snapshot, second: Snapshot) -> tuple[str, str]:
    first_digest = business_digest(first)
    second_digest = business_digest(second)
    first_coverage = source_coverage_digest(first)
    second_coverage = source_coverage_digest(second)
    if first_digest == second_digest and first_coverage == second_coverage:
        return first_digest, second_digest
    counts: dict[str, int] = {}
    if first_coverage != second_coverage:
        counts["source_coverage"] = 1
    account = _changed(first.profile["account"], second.profile["account"])
    if account:
        counts["account"] = account
    roster = _changed(first.roster["characters"], second.roster["characters"])
    if roster:
        counts["roster"] = roster
    common = set(first.builds) & set(second.builds)
    for field in ("progression", "light_cone", "relics", "panel", "panel_context"):
        count = sum(
            first.builds[item].get(field) != second.builds[item].get(field) for item in common
        )
        if count:
            counts[field] = count
    build_ids = len(set(first.builds) ^ set(second.builds))
    if build_ids:
        counts["build_ids"] = build_ids
    summary = ",".join(f"{key}:{counts[key]}" for key in sorted(counts))
    raise SnapshotContentDrift(f"snapshot drift categories and counts: {summary or 'unknown:1'}")
