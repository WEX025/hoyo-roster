"""Stable, privacy-safe failures."""


class SyncError(Exception):
    code = "SYNC_ERROR"


class ConfigurationError(SyncError):
    code = "CONFIGURATION_ERROR"


class LoginRequired(SyncError):
    code = "LOGIN_REQUIRED"


class AuthenticationError(SyncError):
    code = "AUTHENTICATION_ERROR"


class RiskControlError(SyncError):
    code = "RISK_CONTROL_ERROR"


class RateLimitedError(RiskControlError):
    code = "RATE_LIMITED"


class ApiResponseError(SyncError):
    code = "API_RESPONSE_ERROR"


class RequestTransportError(ApiResponseError):
    """The request failed before a validated HTTP response was available."""

    code = "REQUEST_TRANSPORT_ERROR"


class ExpectedUidNotFound(SyncError):
    code = "EXPECTED_UID_NOT_FOUND"


class ExpectedUidAmbiguous(SyncError):
    code = "EXPECTED_UID_AMBIGUOUS"


class OfficialRequestNotObserved(SyncError):
    code = "OFFICIAL_CULTIVATION_REQUEST_NOT_OBSERVED"


class UnsupportedMultiPageContract(SyncError):
    code = "UNSUPPORTED_MULTI_PAGE_CONTRACT"


class RosterCompletenessError(SyncError):
    code = "ROSTER_COMPLETENESS_ERROR"


class SnapshotConsistencyError(SyncError):
    code = "SNAPSHOT_CONSISTENCY_ERROR"


class SnapshotContentDrift(SnapshotConsistencyError):
    code = "SNAPSHOT_CONTENT_DRIFT"


class LifecycleRegression(SyncError):
    code = "LIFECYCLE_REGRESSION"

    def __init__(
        self, message: str, *, categories: tuple[str, ...] = (), details: tuple[str, ...] = ()
    ) -> None:
        super().__init__(message)
        self.categories = categories
        self.details = details


class PartialFieldCoverageRegression(SyncError):
    code = "PARTIAL_FIELD_COVERAGE_REGRESSION"


class CompleteToPartialForbidden(SyncError):
    code = "COMPLETE_TO_PARTIAL_DOWNGRADE_FORBIDDEN"


class FormalRepositoryPrevalidationError(SyncError):
    code = "FORMAL_REPOSITORY_PREVALIDATION_ERROR"


class CandidateValidationError(SyncError):
    code = "CANDIDATE_VALIDATION_ERROR"


class CandidateCleanupError(SyncError):
    code = "CANDIDATE_CLEANUP_ERROR"


class ApplyError(SyncError):
    code = "APPLY_ERROR"


class ApplyRecoveryRequired(ApplyError):
    code = "APPLY_RECOVERY_REQUIRED"


class PublishError(SyncError):
    code = "PUBLISH_ERROR"


class PublishConfigurationError(PublishError):
    code = "PUBLISH_CONFIGURATION_ERROR"


class PublishPathClosureError(PublishError):
    code = "PUBLISH_PATH_CLOSURE_ERROR"


class RemoteMainMoved(PublishError):
    code = "REMOTE_MAIN_MOVED"


class DirectMainPushRejected(PublishError):
    code = "DIRECT_MAIN_PUSH_REJECTED"


class PublishCleanupError(PublishError):
    code = "PUBLISH_CLEANUP_ERROR"
