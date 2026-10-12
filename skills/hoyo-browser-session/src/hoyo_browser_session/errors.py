"""Stable privacy-safe broker errors."""

from __future__ import annotations


class BrokerError(Exception):
    code = "JOB_FAILED"


class ConfigurationError(BrokerError):
    code = "RUNTIME_NOT_READY"


class DiagnosticProfileOverlap(ConfigurationError):
    code = "DIAGNOSTIC_PROFILE_OVERLAP"


class RuntimeNotReady(ConfigurationError):
    code = "RUNTIME_METADATA_INVALID"


class ExternalStateUnsafe(ConfigurationError):
    code = "RUNTIME_METADATA_INVALID"


class BrokerDatabaseError(BrokerError):
    code = "BROKER_DB_ERROR"


class ProtocolStateError(BrokerError):
    code = "BROKER_PROTOCOL_ERROR"


class WorkerStartFailed(BrokerError):
    code = "WORKER_START_FAILED"


class OwnerRuntimeRestartRequired(BrokerError):
    code = "OWNER_RUNTIME_RESTART_REQUIRED"


class OwnerBroken(BrokerError):
    code = "OWNER_BROKEN"


class OwnerShutdownInProgress(BrokerError):
    code = "OWNER_SHUTDOWN_IN_PROGRESS"


class OwnerQueueNotEmpty(BrokerError):
    code = "OWNER_QUEUE_NOT_EMPTY"


class ExecutorNotRegistered(BrokerError):
    code = "EXECUTOR_NOT_REGISTERED"


class ProfileInUse(BrokerError):
    code = "PROFILE_IN_USE"


class ProfileStaleSingleton(BrokerError):
    code = "PROFILE_STALE_SINGLETON"


class ProfileBroken(BrokerError):
    code = "PROFILE_BROKEN"


class ContextNotReusable(BrokerError):
    code = "OWNER_BROKEN"


class AuthenticationRequired(BrokerError):
    code = "AUTHENTICATION_REQUIRED"


class LoginTimeout(BrokerError):
    code = "LOGIN_TIMEOUT"


class LoginNetworkError(BrokerError):
    code = "LOGIN_NETWORK_ERROR"


class LoginBindingFailed(BrokerError):
    code = "LOGIN_BINDING_FAILED"


class WrongAccount(BrokerError):
    code = "WRONG_ACCOUNT"


class RateLimited(BrokerError):
    code = "RATE_LIMITED"


class RiskControl(BrokerError):
    code = "RISK_CONTROL"


class BootstrapIdentityAlreadyEstablished(BrokerError):
    code = "BOOTSTRAP_IDENTITY_ALREADY_ESTABLISHED"


class ExpectedIdentityConflict(BrokerError):
    code = "EXPECTED_IDENTITY_CONFLICT"


class JobAbandoned(BrokerError):
    code = "JOB_ABANDONED"


class JobFailed(BrokerError):
    code = "JOB_FAILED"


class PublishOutcomeUnknown(BrokerError):
    code = "PUBLISH_OUTCOME_UNKNOWN"


class UncertainJobAcknowledgementRejected(BrokerError):
    code = "UNCERTAIN_JOB_ACKNOWLEDGEMENT_REJECTED"


class ActiveOwnerAuthority(BrokerError):
    code = "ACTIVE_OWNER_AUTHORITY"


class JobWaitTimeout(BrokerError):
    code = "JOB_WAIT_TIMEOUT"


def public_error(error: BaseException) -> tuple[str, str]:
    if isinstance(error, BrokerError):
        return error.code, _PUBLIC_MESSAGES.get(
            error.code,
            "broker operation failed; inspect privacy-safe doctor state",
        )
    return "JOB_FAILED", "unexpected internal failure"


_PUBLIC_MESSAGES = {
    "ACTIVE_OWNER_AUTHORITY": "active Owner authority blocks this operation",
    "AUTHENTICATION_REQUIRED": "interactive authentication is required",
    "BOOTSTRAP_IDENTITY_ALREADY_ESTABLISHED": "bootstrap identity is already established",
    "BROKER_DB_ERROR": "broker database operation failed",
    "BROKER_PROTOCOL_ERROR": "broker protocol state is invalid",
    "DIAGNOSTIC_PROFILE_OVERLAP": "diagnostic Profile overlaps shared browser authority",
    "EXECUTOR_NOT_REGISTERED": "operation executor is not registered",
    "EXPECTED_IDENTITY_CONFLICT": "verified account identity conflicts with expected identity",
    "JOB_ABANDONED": "Job was abandoned after Owner loss",
    "JOB_FAILED": "Job execution failed",
    "JOB_WAIT_TIMEOUT": "Job wait timed out",
    "LOGIN_TIMEOUT": "interactive authentication timed out",
    "LOGIN_NETWORK_ERROR": "official login service could not be reached",
    "LOGIN_BINDING_FAILED": "official account Binding could not be validated",
    "OWNER_BROKEN": "Browser Owner is broken and requires explicit recovery",
    "OWNER_QUEUE_NOT_EMPTY": "Browser Owner queue is not empty",
    "OWNER_RUNTIME_RESTART_REQUIRED": "Browser Owner Runtime does not match the active Runtime",
    "OWNER_SHUTDOWN_IN_PROGRESS": "Browser Owner shutdown is in progress",
    "PROFILE_BROKEN": "browser Profile is broken and requires explicit recovery",
    "PROFILE_IN_USE": "browser Profile is already in use",
    "PROFILE_STALE_SINGLETON": "browser Profile contains stale singleton state",
    "PUBLISH_OUTCOME_UNKNOWN": "publish outcome requires reconciliation",
    "RATE_LIMITED": "account requests are rate limited",
    "RISK_CONTROL": "account requests are blocked by risk control",
    "RUNTIME_METADATA_INVALID": "active Runtime metadata is invalid or unavailable",
    "UNCERTAIN_JOB_ACKNOWLEDGEMENT_REJECTED": "uncertain Job acknowledgement was rejected",
    "WORKER_START_FAILED": "Browser Owner could not be started",
    "WRONG_ACCOUNT": "authenticated account does not match the expected account",
}
