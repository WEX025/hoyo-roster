"""Privacy-safe fixed error reporting; never print raw exception values."""

from hoyo_roster.errors import OperationError
from .errors import SyncError


def public_error(exc: BaseException) -> tuple[str, str]:
    """Return a stable error code and a message that cannot expose exception repr."""

    if isinstance(exc, OperationError):
        return exc.code, str(exc)
    if isinstance(exc, SyncError):
        return exc.code, str(exc)
    return "UNEXPECTED_ERROR", "unexpected internal failure"
