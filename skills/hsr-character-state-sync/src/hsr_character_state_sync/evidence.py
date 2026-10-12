"""HSR-owned fixed Evidence production and validation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from hoyo_browser_session.models import ExecutorResult, StableCode

from .model import BuildCoverageStatus, RosterCoverageStatus

GAME = "hsr"
PUBLISH_OPERATION = "publish-main"
BOOTSTRAP_OPERATION = "bootstrap-check"
DRY_RUN_OPERATION = "dry-run"
CURRENT_CONTRACT_VERSION = 1


class HsrEvidenceCode(StrEnum):
    FORMAL_DATA_UNCHANGED = "FORMAL_DATA_UNCHANGED"
    AVATAR_LIST_DIAGNOSTICS_EQUAL = "HSR_AVATAR_LIST_DIAGNOSTICS_EQUAL"
    BOOTSTRAP_PASS = "HSR_BOOTSTRAP_PASS"
    BUILD_COVERAGE_OBSERVED = "HSR_BUILD_COVERAGE_OBSERVED"
    BUILD_COVERAGE_UNAVAILABLE_NO_ROSTER = "HSR_BUILD_COVERAGE_UNAVAILABLE_NO_ROSTER"
    BUSINESS_DIGEST_EQUAL = "HSR_BUSINESS_DIGEST_EQUAL"
    BUSINESS_PASS_COUNT_0 = "HSR_BUSINESS_PASS_COUNT_0"
    BUSINESS_PASS_COUNT_2 = "HSR_BUSINESS_PASS_COUNT_2"
    CANDIDATE_REMOVED = "HSR_CANDIDATE_REMOVED"
    CANDIDATE_VALIDATOR_PASS = "HSR_CANDIDATE_VALIDATOR_PASS"
    CANDIDATE_VALIDATOR_SKIPPED_NO_OP = "HSR_CANDIDATE_VALIDATOR_SKIPPED_NO_OP"
    DRY_RUN_PASS = "HSR_DRY_RUN_PASS"
    EXACT_BINDING_PASS = "HSR_EXACT_BINDING_PASS"
    EXPECTED_REGION_MATCH = "HSR_EXPECTED_REGION_MATCH"
    PAGE_SEALED = "HSR_PAGE_SEALED"
    PROFILE_COVERAGE_OBSERVED = "HSR_PROFILE_COVERAGE_OBSERVED"
    ROSTER_COVERAGE_EQUAL = "HSR_ROSTER_COVERAGE_EQUAL"
    ROSTER_COVERAGE_OBSERVED_NONEMPTY = "HSR_ROSTER_COVERAGE_OBSERVED_NONEMPTY"
    ROSTER_COVERAGE_UNAVAILABLE_ZERO_OWNED_OVERLAY = (
        "HSR_ROSTER_COVERAGE_UNAVAILABLE_ZERO_OWNED_OVERLAY"
    )
    SESSION_TRANSPORT_READY = "HSR_SESSION_TRANSPORT_READY"
    SNAPSHOT_PARTIAL = "HSR_SNAPSHOT_PARTIAL"
    SOURCE_COVERAGE_DIGEST_EQUAL = "HSR_SOURCE_COVERAGE_DIGEST_EQUAL"


_BOOTSTRAP_EVIDENCE = frozenset(
    {
        HsrEvidenceCode.FORMAL_DATA_UNCHANGED.value,
        HsrEvidenceCode.BOOTSTRAP_PASS.value,
        HsrEvidenceCode.BUSINESS_PASS_COUNT_0.value,
        HsrEvidenceCode.EXACT_BINDING_PASS.value,
        HsrEvidenceCode.EXPECTED_REGION_MATCH.value,
        HsrEvidenceCode.PAGE_SEALED.value,
        HsrEvidenceCode.SESSION_TRANSPORT_READY.value,
    }
)
_DRY_RUN_COMMON_EVIDENCE = frozenset(
    {
        HsrEvidenceCode.FORMAL_DATA_UNCHANGED.value,
        HsrEvidenceCode.AVATAR_LIST_DIAGNOSTICS_EQUAL.value,
        HsrEvidenceCode.BUSINESS_DIGEST_EQUAL.value,
        HsrEvidenceCode.BUSINESS_PASS_COUNT_2.value,
        HsrEvidenceCode.CANDIDATE_REMOVED.value,
        HsrEvidenceCode.DRY_RUN_PASS.value,
        HsrEvidenceCode.PROFILE_COVERAGE_OBSERVED.value,
        HsrEvidenceCode.ROSTER_COVERAGE_EQUAL.value,
        HsrEvidenceCode.SNAPSHOT_PARTIAL.value,
        HsrEvidenceCode.SOURCE_COVERAGE_DIGEST_EQUAL.value,
    }
)
_DRY_RUN_COVERAGE_EVIDENCE = (
    frozenset(
        {
            HsrEvidenceCode.ROSTER_COVERAGE_OBSERVED_NONEMPTY.value,
            HsrEvidenceCode.BUILD_COVERAGE_OBSERVED.value,
        }
    ),
    frozenset(
        {
            HsrEvidenceCode.ROSTER_COVERAGE_UNAVAILABLE_ZERO_OWNED_OVERLAY.value,
            HsrEvidenceCode.BUILD_COVERAGE_UNAVAILABLE_NO_ROSTER.value,
        }
    ),
)


def validate_hsr_operation_result(
    operation: str,
    result: ExecutorResult,
    *,
    contract_version: int,
) -> None:
    """Require the exact HSR business Evidence contract for one fixed operation."""

    if contract_version != 1:
        raise ValueError("HSR operation contract version was unsupported")
    if result.stable_code is not StableCode.PASS:
        return
    evidence = frozenset(result.evidence_codes)
    if operation == PUBLISH_OPERATION:
        if evidence:
            raise ValueError("HSR publish result evidence was invalid")
        return
    if operation == BOOTSTRAP_OPERATION:
        if (
            evidence != _BOOTSTRAP_EVIDENCE
            or not result.no_op
            or result.data_modified
            or result.commit_sha is not None
        ):
            raise ValueError("HSR bootstrap operation evidence was incomplete")
        return
    if operation == DRY_RUN_OPERATION:
        candidate_evidence = (
            HsrEvidenceCode.CANDIDATE_VALIDATOR_SKIPPED_NO_OP.value
            if result.no_op
            else HsrEvidenceCode.CANDIDATE_VALIDATOR_PASS.value
        )
        expected_sets = tuple(
            _DRY_RUN_COMMON_EVIDENCE | coverage | frozenset({candidate_evidence})
            for coverage in _DRY_RUN_COVERAGE_EVIDENCE
        )
        if evidence not in expected_sets or result.data_modified or result.commit_sha is not None:
            raise ValueError("HSR dry-run operation evidence was incomplete")
        return
    raise ValueError("HSR Evidence operation was not registered")


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

    def evidence_codes(self, operation: str) -> tuple[str, ...]:
        codes: set[HsrEvidenceCode] = set()
        bootstrap_complete = all(
            (
                self.exact_binding,
                self.expected_region_match,
                self.cultivation_session_bootstrap,
                self.session_transport_ready,
                self.page_sealed,
                self.formal_data_unchanged,
            )
        )
        if operation == BOOTSTRAP_OPERATION:
            if bootstrap_complete:
                codes.update(
                    {
                        HsrEvidenceCode.FORMAL_DATA_UNCHANGED,
                        HsrEvidenceCode.BOOTSTRAP_PASS,
                        HsrEvidenceCode.EXACT_BINDING_PASS,
                        HsrEvidenceCode.EXPECTED_REGION_MATCH,
                        HsrEvidenceCode.PAGE_SEALED,
                        HsrEvidenceCode.SESSION_TRANSPORT_READY,
                    }
                )
            if self.business_pass_count == 0:
                codes.add(HsrEvidenceCode.BUSINESS_PASS_COUNT_0)
            no_op = True
        elif operation == DRY_RUN_OPERATION:
            if bootstrap_complete:
                codes.update(
                    {
                        HsrEvidenceCode.FORMAL_DATA_UNCHANGED,
                        HsrEvidenceCode.DRY_RUN_PASS,
                    }
                )
            if self.business_pass_count == 2:
                codes.add(HsrEvidenceCode.BUSINESS_PASS_COUNT_2)
            if self.profile_coverage == "OBSERVED":
                codes.add(HsrEvidenceCode.PROFILE_COVERAGE_OBSERVED)
            if self.roster_coverage is RosterCoverageStatus.OBSERVED_NONEMPTY:
                codes.add(HsrEvidenceCode.ROSTER_COVERAGE_OBSERVED_NONEMPTY)
            elif self.roster_coverage is RosterCoverageStatus.UNAVAILABLE_ZERO_OWNED_OVERLAY:
                codes.add(HsrEvidenceCode.ROSTER_COVERAGE_UNAVAILABLE_ZERO_OWNED_OVERLAY)
            if self.build_coverage is BuildCoverageStatus.OBSERVED:
                codes.add(HsrEvidenceCode.BUILD_COVERAGE_OBSERVED)
            elif self.build_coverage is BuildCoverageStatus.UNAVAILABLE_NO_ROSTER:
                codes.add(HsrEvidenceCode.BUILD_COVERAGE_UNAVAILABLE_NO_ROSTER)
            if self.roster_coverage_equal:
                codes.add(HsrEvidenceCode.ROSTER_COVERAGE_EQUAL)
            if self.avatar_list_diagnostics_equal:
                codes.add(HsrEvidenceCode.AVATAR_LIST_DIAGNOSTICS_EQUAL)
            if self.source_coverage_digest_equal:
                codes.add(HsrEvidenceCode.SOURCE_COVERAGE_DIGEST_EQUAL)
            if self.business_digest_equal:
                codes.add(HsrEvidenceCode.BUSINESS_DIGEST_EQUAL)
            if self.snapshot_state == "partial":
                codes.add(HsrEvidenceCode.SNAPSHOT_PARTIAL)
            if self.candidate_validator == "PASS":
                codes.add(HsrEvidenceCode.CANDIDATE_VALIDATOR_PASS)
                no_op = False
            elif self.candidate_validator == "SKIPPED_NO_OP":
                codes.add(HsrEvidenceCode.CANDIDATE_VALIDATOR_SKIPPED_NO_OP)
                no_op = True
            else:
                no_op = False
            if self.candidate_removed:
                codes.add(HsrEvidenceCode.CANDIDATE_REMOVED)
        else:
            raise ValueError("HSR Evidence operation was not registered")
        values = tuple(sorted(code.value for code in codes))
        validate_hsr_operation_result(
            operation,
            ExecutorResult(
                stable_code=StableCode.PASS,
                no_op=no_op,
                evidence_codes=values,
            ),
            contract_version=CURRENT_CONTRACT_VERSION,
        )
        return values
