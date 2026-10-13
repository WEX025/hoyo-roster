from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Snapshot:
    profile: dict[str, Any]
    roster: dict[str, Any]
    builds: dict[str, dict[str, Any]]
    weapon_count: int
    source_semantic_digest: str | None = None
