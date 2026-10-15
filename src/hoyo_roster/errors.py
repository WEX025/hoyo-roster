"""Privacy-safe shared operation errors."""


class OperationError(Exception):
    code = "LOCAL_OPERATION_FAILED"


class ConfigurationError(OperationError):
    code = "CONFIGURATION_INVALID"


class DiagnosticProfileOverlap(ConfigurationError):
    code = "DIAGNOSTIC_PROFILE_OVERLAP"


class BrowserUnavailable(ConfigurationError):
    code = "BROWSER_UNAVAILABLE"


class BrowserProfileInvalid(ConfigurationError):
    code = "BROWSER_PROFILE_INVALID"


class ContextNotReusable(OperationError):
    code = "BROWSER_CONTEXT_UNAVAILABLE"


class AuthenticationRequired(OperationError):
    code = "AUTHENTICATION_REQUIRED"


class LoginTimeout(OperationError):
    code = "LOGIN_TIMEOUT"


class LoginNetworkError(OperationError):
    code = "LOGIN_NETWORK_ERROR"


class LoginBindingFailed(OperationError):
    code = "LOGIN_BINDING_FAILED"


class WrongAccount(OperationError):
    code = "WRONG_ACCOUNT"


class RateLimited(OperationError):
    code = "RATE_LIMITED"


class RiskControl(OperationError):
    code = "RISK_CONTROL"


class ExpectedIdentityConflict(OperationError):
    code = "EXPECTED_IDENTITY_CONFLICT"


def public_error(error: BaseException) -> tuple[str, str]:
    return (
        (error.code, "See docs/local-use.md for recovery")
        if isinstance(error, OperationError)
        else ("LOCAL_OPERATION_FAILED", "See docs/local-use.md")
    )


class LoginCancelled(OperationError):
    code = "LOGIN_CANCELLED"


class LoginSessionInitializationFailed(OperationError):
    code = "LOGIN_SESSION_INITIALIZATION_FAILED"


class LoginConsentRequired(OperationError):
    code = "LOGIN_CONSENT_REQUIRED"


class LoginControlUnavailable(OperationError):
    code = "LOGIN_CONTROL_UNAVAILABLE"


class LoginMethodUnavailable(OperationError):
    code = "LOGIN_METHOD_UNAVAILABLE"


class SmsReceiptUnknown(OperationError):
    code = "SMS_RECEIPT_UNKNOWN"


class SmsRequestRejected(OperationError):
    code = "SMS_REQUEST_REJECTED"


class PhoneInputInvalid(OperationError):
    code = "PHONE_INPUT_INVALID"


class OtpInputInvalid(OperationError):
    code = "OTP_INPUT_INVALID"


class RequestScheduleInvalid(ConfigurationError):
    code = "REQUEST_SCHEDULE_INVALID"
