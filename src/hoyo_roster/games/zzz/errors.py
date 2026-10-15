"""Fail-closed error types with stable, non-sensitive codes."""

from __future__ import annotations


class SyncError(Exception):
    """Base error whose public message must never contain request data."""

    code = "SYNC_ERROR"


class ConfigurationError(SyncError):
    code = "CONFIGURATION_ERROR"


class ApiResponseError(SyncError):
    code = "API_RESPONSE_ERROR"


class SnapshotConsistencyError(SyncError):
    code = "SNAPSHOT_CONSISTENCY_ERROR"


class SnapshotContentDrift(SnapshotConsistencyError):
    code = "SNAPSHOT_CONTENT_DRIFT"


class UnverifiedPaginationContract(SnapshotConsistencyError):
    code = "UNVERIFIED_PAGINATION_CONTRACT"


class MappingError(SyncError):
    code = "MAPPING_ERROR"


class AccountIdentityMismatch(SyncError):
    code = "ACCOUNT_IDENTITY_MISMATCH"


class LifecycleRegressionError(SyncError):
    code = "LIFECYCLE_REGRESSION"


class SyncedAtRegressionError(SyncError):
    code = "SYNCED_AT_REGRESSION"


class CandidateValidationError(SyncError):
    code = "CANDIDATE_VALIDATION_ERROR"

    def __init__(
        self,
        message: str,
        *,
        diagnostic_sha256: str | None = None,
    ) -> None:
        super().__init__(message)
        self.diagnostic_sha256 = diagnostic_sha256


class FormalRepositoryPrevalidationError(CandidateValidationError):
    code = "FORMAL_REPOSITORY_PREVALIDATION_ERROR"


class CandidateCleanupError(SyncError):
    code = "CANDIDATE_CLEANUP_ERROR"


class CandidateResidueError(SyncError):
    code = "CANDIDATE_RESIDUE_ERROR"


class CandidateInspectionError(SyncError):
    code = "CANDIDATE_INSPECTION_ERROR"
