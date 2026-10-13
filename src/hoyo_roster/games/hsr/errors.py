"""Stable, privacy-safe synchronization failures."""

from __future__ import annotations


class SyncError(Exception):
    code = "SYNC_ERROR"


class ConfigurationError(SyncError):
    code = "CONFIGURATION_ERROR"


class LoginRequired(SyncError):
    code = "LOGIN_REQUIRED"


class AuthenticationRequired(LoginRequired):
    code = "AUTHENTICATION_REQUIRED"


class AuthenticationError(SyncError):
    code = "AUTHENTICATION_ERROR"


class WrongAccount(SyncError):
    code = "WRONG_ACCOUNT"


class RiskControlError(SyncError):
    code = "RISK_CONTROL_ERROR"


class RateLimitedError(RiskControlError):
    code = "RATE_LIMITED"


class SessionInitializationError(SyncError):
    code = "SESSION_INITIALIZATION_ERROR"


class CultivationSessionMismatch(SessionInitializationError):
    code = "CULTIVATION_SESSION_MISMATCH"


class BrowserLaunchError(ConfigurationError):
    code = "BROWSER_LAUNCH_FAILED_OTHER"
    diagnostics: object | None = None


class BrowserExecutableMissing(BrowserLaunchError):
    code = "BROWSER_EXECUTABLE_MISSING"


class BrowserProfileInUse(BrowserLaunchError):
    code = "BROWSER_PROFILE_IN_USE"


class BrowserProfileStaleSingleton(BrowserLaunchError):
    code = "BROWSER_PROFILE_STALE_SINGLETON"


class BrowserProfilePermissionDenied(BrowserLaunchError):
    code = "BROWSER_PROFILE_PERMISSION_DENIED"


class BrowserProfileInvalid(BrowserLaunchError):
    code = "BROWSER_PROFILE_INVALID"


class BrowserChannelProfileIncompatible(BrowserLaunchError):
    code = "BROWSER_CHANNEL_PROFILE_INCOMPATIBLE"


class BrowserProcessExitedDuringLaunch(BrowserLaunchError):
    code = "BROWSER_PROCESS_EXITED_DURING_LAUNCH"


class BrowserSandboxOrPlatformFailure(BrowserLaunchError):
    code = "BROWSER_SANDBOX_OR_PLATFORM_FAILURE"


class BrowserLaunchTimeout(BrowserLaunchError):
    code = "BROWSER_LAUNCH_TIMEOUT"


class BrowserLaunchFailedOther(BrowserLaunchError):
    code = "BROWSER_LAUNCH_FAILED_OTHER"


class ApiResponseError(SyncError):
    code = "API_RESPONSE_ERROR"


class RequestTransportError(ApiResponseError):
    """The request failed before a validated HTTP response was available."""

    code = "REQUEST_TRANSPORT_ERROR"


class ExpectedUidNotFound(SyncError):
    code = "EXPECTED_UID_NOT_FOUND"


class ExpectedUidAmbiguous(SyncError):
    code = "EXPECTED_UID_AMBIGUOUS"


class UnsupportedMultiPageContract(SyncError):
    code = "UNSUPPORTED_MULTI_PAGE_CONTRACT"


class RosterCompletenessError(SyncError):
    code = "ROSTER_COMPLETENESS_ERROR"


class CultivationOwnedRosterEmpty(RosterCompletenessError):
    code = "CULTIVATION_OWNED_ROSTER_EMPTY"

    def __init__(
        self,
        *,
        entry_count: int,
        true_count: int,
        false_count: int,
        response_data_keyset_sha256: str,
        entry_keyset_sha256: str,
    ) -> None:
        super().__init__(
            "cultivation avatar list contained no source-observed owned entries; "
            f"entry_count={entry_count};true_count={true_count};"
            f"false_count={false_count}"
        )
        self.entry_count = entry_count
        self.true_count = true_count
        self.false_count = false_count
        self.response_data_keyset_sha256 = response_data_keyset_sha256
        self.entry_keyset_sha256 = entry_keyset_sha256
        self.query_field_names: tuple[str, ...] = ()
        self.request_header_names: tuple[str, ...] = ()
        self.response_root_keyset_sha256 = ""
        self.page_route_state = "UNAVAILABLE"

    def attach_request_diagnostics(
        self,
        *,
        query_field_names: tuple[str, ...],
        request_header_names: tuple[str, ...],
        response_root_keyset_sha256: str,
        page_route_state: str,
    ) -> None:
        self.query_field_names = query_field_names
        self.request_header_names = request_header_names
        self.response_root_keyset_sha256 = response_root_keyset_sha256
        self.page_route_state = page_route_state


class DetailCoverageError(SyncError):
    code = "DETAIL_COVERAGE_ERROR"


class SnapshotConsistencyError(SyncError):
    code = "SNAPSHOT_CONSISTENCY_ERROR"


class SnapshotContentDrift(SnapshotConsistencyError):
    code = "SNAPSHOT_CONTENT_DRIFT"


class LifecycleRegression(SyncError):
    code = "LIFECYCLE_REGRESSION"


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
