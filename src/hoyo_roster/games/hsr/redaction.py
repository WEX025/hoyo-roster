"""Privacy-safe fixed error reporting; never print raw exception values."""

from hoyo_roster.errors import OperationError
from .errors import SyncError


def public_error(error: BaseException) -> tuple[str, str]:
    if isinstance(error, OperationError):
        return error.code, str(error)
    if isinstance(error, SyncError):
        return error.code, str(error)
    return "UNEXPECTED_ERROR", "unexpected internal failure"
