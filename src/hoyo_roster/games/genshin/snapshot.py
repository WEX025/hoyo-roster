"""Canonical business digest and privacy-safe two-pass comparison."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from .errors import SnapshotContentDrift
from .model import Snapshot


def business_document(snapshot: Snapshot) -> dict[str, Any]:
    return {
        "account": snapshot.profile["account"],
        "roster": snapshot.roster,
        "builds": {key: snapshot.builds[key] for key in sorted(snapshot.builds)},
    }


def business_digest(snapshot: Snapshot) -> str:
    encoded = json.dumps(
        business_document(snapshot),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def roster_id_set_digest(snapshot: Snapshot) -> str:
    character_ids = sorted(snapshot.roster["characters"])
    encoded = json.dumps(
        character_ids, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def roster_business_digest(snapshot: Snapshot) -> str:
    encoded = json.dumps(
        snapshot.roster, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _changed(left: Mapping[str, Any], right: Mapping[str, Any]) -> int:
    return sum(left.get(key) != right.get(key) for key in set(left) | set(right))


def assert_stable(first: Snapshot, second: Snapshot) -> tuple[str, str]:
    first_digest = business_digest(first)
    second_digest = business_digest(second)
    source_semantics_equal = first.source_semantic_digest == second.source_semantic_digest
    if first_digest == second_digest and source_semantics_equal:
        return first_digest, second_digest
    counts: dict[str, int] = {}
    if not source_semantics_equal:
        counts["source_semantics"] = 1
    account = _changed(first.profile["account"], second.profile["account"])
    if account:
        counts["account"] = account
    roster = _changed(first.roster["characters"], second.roster["characters"])
    if roster:
        counts["roster"] = roster
    common = set(first.builds) & set(second.builds)
    for field in ("progression", "weapon", "artifacts", "panel"):
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
