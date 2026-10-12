"""Centralized output redaction."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

from hoyo_roster.errors import OperationError

from .errors import SyncError

_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(cookie|token|ds|device[_-]?id|device[_-]?fp|e_nap_token|authorization)"
    r"\b\s*[:=]\s*([^\s,;]+)"
)
_COOKIE_RE = re.compile(r"(?i)\b(?:cookie|set-cookie)\s*:\s*[^\r\n]+")


def uid_digest(uid: str) -> str:
    """Return a stable short digest without exposing the UID."""

    return hashlib.sha256(uid.encode("utf-8")).hexdigest()[:12]


class Redactor:
    """Redact known values and authentication-shaped assignments."""

    def __init__(
        self,
        *,
        sensitive_values: Iterable[str] = (),
        uid_values: Iterable[str] = (),
    ) -> None:
        self._sensitive = tuple(
            sorted(
                {value for value in sensitive_values if value},
                key=len,
                reverse=True,
            )
        )
        self._uids = tuple(sorted({value for value in uid_values if value}, key=len, reverse=True))

    def redact(self, text: str) -> str:
        result = text
        for uid in self._uids:
            result = result.replace(uid, f"uid_sha256:{uid_digest(uid)}")
        for value in self._sensitive:
            result = result.replace(value, "[REDACTED]")
        result = _COOKIE_RE.sub("Cookie: [REDACTED]", result)
        return _ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", result)


def public_error(exc: BaseException) -> tuple[str, str]:
    """Return a stable error code and a message that cannot expose exception repr."""

    if isinstance(exc, OperationError):
        return exc.code, str(exc)
    if isinstance(exc, SyncError):
        return exc.code, str(exc)
    return "UNEXPECTED_ERROR", "unexpected internal failure"
