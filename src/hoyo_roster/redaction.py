"""Centralized privacy-safe diagnostics and output redaction."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(cookie|token|password|authorization|ds|device[_-]?(?:id|fp)|"
    r"storage[_-]?state)\b\s*[:=]\s*([^\s,;]+)"
)
_COOKIE_HEADER_RE = re.compile(r"(?i)\b(?:cookie|set-cookie)\s*:\s*[^\r\n]+")
_PRIVATE_PATH_RE = re.compile(r"(?<![A-Za-z0-9])(?:/Users|/home)/[^\s,;]+")


def digest_prefix(value: str, *, length: int = 12) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def diagnostic_hash(error: BaseException, *, context: str) -> str:
    material = f"{context}:{type(error).__module__}.{type(error).__qualname__}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class Redactor:
    def __init__(
        self,
        *,
        sensitive_values: Iterable[str] = (),
        identity_values: Iterable[str] = (),
    ) -> None:
        self._sensitive = tuple(
            sorted({value for value in sensitive_values if value}, key=len, reverse=True)
        )
        self._identities = tuple(
            sorted({value for value in identity_values if value}, key=len, reverse=True)
        )

    def redact(self, text: str) -> str:
        result = text
        for value in self._identities:
            result = result.replace(value, f"identity_sha256:{digest_prefix(value)}")
        for value in self._sensitive:
            result = result.replace(value, "[REDACTED]")
        result = _COOKIE_HEADER_RE.sub("Cookie: [REDACTED]", result)
        result = _ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", result)
        return _PRIVATE_PATH_RE.sub("[PRIVATE_PATH]", result)
