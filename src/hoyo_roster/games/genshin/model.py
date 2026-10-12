from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SourcePass:
    binding: dict[str, Any]
    avatars: list[dict[str, Any]]
    request_count: int
    total: int


@dataclass(frozen=True, slots=True)
class Snapshot:
    profile: dict[str, Any]
    roster: dict[str, Any]
    builds: dict[str, dict[str, Any]]
    weapon_count: int
    artifact_slot_count: int = 0
    empty_artifact_slot_count: int = 0
    source_semantic_digest: str | None = None
