from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RosterCoverageStatus(StrEnum):
    OBSERVED_NONEMPTY = "OBSERVED_NONEMPTY"
    UNAVAILABLE_ZERO_OWNED_OVERLAY = "UNAVAILABLE_ZERO_OWNED_OVERLAY"


class BuildCoverageStatus(StrEnum):
    OBSERVED = "OBSERVED"
    UNAVAILABLE_NO_ROSTER = "UNAVAILABLE_NO_ROSTER"


@dataclass(frozen=True, slots=True)
class SnapshotDiagnostics:
    detail_count: int
    recommend_count: int
    light_cone_equipped_count: int
    light_cone_unequipped_count: int
    relic_equipped_slot_count: int
    relic_empty_slot_count: int
    panel_character_count: int
    observed_panel_property_ids: tuple[str, ...]
    observed_extra_top_level_point_types: tuple[str, ...]
    unmapped_public_point_count: int


@dataclass(frozen=True, slots=True)
class Snapshot:
    profile: dict[str, Any]
    roster: dict[str, Any]
    builds: dict[str, dict[str, Any]]
    diagnostics: SnapshotDiagnostics
    source_coverage: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AvatarListObservation:
    query_field_names: tuple[str, ...]
    request_header_names: tuple[str, ...]
    response_root_keyset_sha256: str
    response_data_keyset_sha256: str
    entry_keyset_sha256: str
    entry_count: int
    is_own_true_count: int
    is_own_false_count: int
    pagination_signal_count: int

    def __post_init__(self) -> None:
        counts = (
            self.entry_count,
            self.is_own_true_count,
            self.is_own_false_count,
            self.pagination_signal_count,
        )
        digests = (
            self.response_root_keyset_sha256,
            self.response_data_keyset_sha256,
            self.entry_keyset_sha256,
        )
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError("avatar-list observation counts were invalid")
        if self.entry_count != self.is_own_true_count + self.is_own_false_count:
            raise ValueError("avatar-list strict ownership counts were inconsistent")
        if self.pagination_signal_count:
            raise ValueError("avatar-list observation exposed pagination")
        if any(
            len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in digests
        ):
            raise ValueError("avatar-list observation digest was invalid")

    def coverage_document(self) -> dict[str, Any]:
        return {
            "query_field_names": list(self.query_field_names),
            "request_header_names": list(self.request_header_names),
            "response_root_keyset_sha256": self.response_root_keyset_sha256,
            "response_data_keyset_sha256": self.response_data_keyset_sha256,
            "entry_keyset_sha256": self.entry_keyset_sha256,
            "entry_count": self.entry_count,
            "is_own_true_count": self.is_own_true_count,
            "is_own_false_count": self.is_own_false_count,
            "pagination_signal_count": self.pagination_signal_count,
        }


@dataclass(frozen=True, slots=True, repr=False)
class PassObservation:
    binding: dict[str, Any] = field(repr=False)
    roster_coverage: RosterCoverageStatus
    build_coverage: BuildCoverageStatus
    avatar_list: AvatarListObservation
    request_count: int
    snapshot: Snapshot | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if type(self.request_count) is not int or self.request_count < 2:
            raise ValueError("Business Pass request count was invalid")
        if self.roster_coverage is RosterCoverageStatus.OBSERVED_NONEMPTY:
            if (
                self.build_coverage is not BuildCoverageStatus.OBSERVED
                or not self.avatar_list.is_own_true_count
                or self.snapshot is None
            ):
                raise ValueError("observed Business Pass coverage was inconsistent")
        elif (
            self.build_coverage is not BuildCoverageStatus.UNAVAILABLE_NO_ROSTER
            or self.avatar_list.is_own_true_count
            or self.snapshot is not None
        ):
            raise ValueError("unavailable Business Pass coverage was inconsistent")

    def coverage_document(self) -> dict[str, Any]:
        return {
            "profile_coverage": "OBSERVED",
            "roster_coverage": self.roster_coverage.value,
            "build_coverage": self.build_coverage.value,
            "avatar_list": self.avatar_list.coverage_document(),
        }
