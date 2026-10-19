"""Fixed public errors; never include source payloads or credential values."""


class OperationError(Exception):
    code = "LOCAL_OPERATION_FAILED"
    stop_account = False


class AuthenticationRecoveryFailed(OperationError):
    code = "AUTH_RECOVERY_FAILED"
    stop_account = True


class ConfigurationError(OperationError):
    code = "CONFIGURATION_INVALID"


class AuthenticationRequired(OperationError):
    code = "AUTHENTICATION_REQUIRED"


class ClientBrowserRequired(OperationError):
    code = "CLIENT_BROWSER_REQUIRED"


class ClientProtocolError(OperationError):
    code = "CLIENT_PROTOCOL_ERROR"


class ClientSourceUnavailable(OperationError):
    code = "CLIENT_SOURCE_UNAVAILABLE"

    def __init__(self, missing_sources=()):
        self.missing_sources = tuple(missing_sources)
        super().__init__()


class SourceAdapterMissing(ClientSourceUnavailable):
    code = "SOURCE_ADAPTER_MISSING"


class SourceDataInvalid(OperationError):
    code = "SOURCE_DATA_INVALID"


class SourceRequestNotObserved(OperationError):
    code = "SOURCE_REQUEST_NOT_OBSERVED"


class LoginTimeout(OperationError):
    code = "LOGIN_TIMEOUT"


class LoginCancelled(OperationError):
    code = "LOGIN_CANCELLED"


class WrongAccount(OperationError):
    code = "WRONG_ACCOUNT"


class RateLimited(OperationError):
    code = "RATE_LIMITED"


class RiskControl(OperationError):
    code = "RISK_CONTROL"


class RequestScheduleInvalid(ConfigurationError):
    code = "REQUEST_SCHEDULE_INVALID"


class WorkspaceError(ValueError):
    """Only fixed codes may be raised at the public boundary."""
