"""Centralized output redaction."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

from hoyo_browser_session.errors import BrokerError

from .errors import SyncError

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(cookie|token|ds|device[_-]?(?:id|fp)|authorization)\b\s*[:=]\s*([^\s,;]+)"
)
_COOKIE_HEADER = re.compile(r"(?i)\b(?:cookie|set-cookie)\s*:\s*[^\r\n]+")


def digest_prefix(value: str, *, length: int = 12) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


class Redactor:
    def __init__(self, values: Iterable[str] = ()) -> None:
        self._values = tuple(sorted({item for item in values if item}, key=len, reverse=True))

    def redact(self, text: str) -> str:
        result = text
        for value in self._values:
            result = result.replace(value, "[REDACTED]")
        result = _COOKIE_HEADER.sub("Cookie: [REDACTED]", result)
        return _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", result)


def public_error(error: BaseException) -> tuple[str, str]:
    if isinstance(error, BrokerError):
        return error.code, str(error)
    if isinstance(error, SyncError):
        return error.code, str(error)
    return "UNEXPECTED_ERROR", "unexpected internal failure"
