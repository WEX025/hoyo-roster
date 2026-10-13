"""HSR collection coverage evidence."""

from dataclasses import dataclass
from typing import Literal
from .model import BuildCoverageStatus, RosterCoverageStatus


@dataclass(slots=True)
class SyncEvidence:
    exact_binding: bool = False
    expected_region_match: bool = False
    cultivation_session_bootstrap: bool = False
    session_transport_ready: bool = False
    page_sealed: bool = False
    business_pass_count: int = 0
    formal_data_unchanged: bool = False
    profile_coverage: Literal["OBSERVED"] | None = None
    roster_coverage: RosterCoverageStatus | None = None
    build_coverage: BuildCoverageStatus | None = None
    roster_coverage_equal: bool = False
    avatar_list_diagnostics_equal: bool = False
    source_coverage_digest_equal: bool = False
    business_digest_equal: bool = False
    snapshot_state: Literal["partial"] | None = None
    candidate_validator: Literal["PASS", "SKIPPED_NO_OP"] | None = None
    candidate_removed: bool = False
