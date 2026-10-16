"""A bounded, correlated bridge to the AI client's live browser host; no browser driver."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .errors import (
    ClientProtocolError,
    ClientSourceUnavailable,
    SourceAdapterMissing,
    OperationError,
    SourceDataInvalid,
    SourceRequestNotObserved,
)
from .jsonio import loads_json, json_bytes

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 16 * 1024 * 1024
HOST_ERRORS = {
    "AUTHENTICATION_REQUIRED",
    "LOGIN_CANCELLED",
    "LOGIN_TIMEOUT",
    "CLIENT_BROWSER_REQUIRED",
    "CLIENT_BROWSER_UNAVAILABLE",
    "CLIENT_SOURCE_UNAVAILABLE",
    "SOURCE_ADAPTER_MISSING",
    "SOURCE_DATA_INVALID",
    "SOURCE_REQUEST_NOT_OBSERVED",
    "CLIENT_PAGE_UNAVAILABLE",
    "CLIENT_PROTOCOL_ERROR",
    "CLIENT_REQUEST_FAILED",
    "RATE_LIMITED",
    "RISK_CONTROL",
    "WRONG_ACCOUNT",
}


class ClientPort(Protocol):
    async def exchange(self, message: dict) -> dict: ...


class StdioPort:
    """Private pipe only. Frames must never be sent to a user-visible terminal."""

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer

    async def exchange(self, message):
        encoded = json_bytes(message, compact=True)
        if len(encoded) > MAX_FRAME_BYTES:
            raise ClientProtocolError()
        self.writer.write(encoded.rstrip(b"\n") + b"\n")
        self.writer.flush()
        raw = self.reader.readline(MAX_FRAME_BYTES + 1)
        if not raw or len(raw) > MAX_FRAME_BYTES or not raw.endswith(b"\n"):
            raise ClientProtocolError()
        try:
            return loads_json(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise ClientProtocolError() from None


@dataclass(repr=False)
class ClientSession:
    port: ClientPort = field(repr=False)
    session_ref: str | None = field(default=None, init=False, repr=False)
    source_reader: bool = field(default=False, init=False)
    _sequence: int = field(default=0, init=False)

    async def call(self, method: str, params: dict | None = None):
        self._sequence += 1
        message = {
            "version": PROTOCOL_VERSION,
            "id": self._sequence,
            "method": method,
            "session": self.session_ref,
            "params": params or {},
        }
        reply = await self.port.exchange(message)
        if (
            not isinstance(reply, dict)
            or set(reply) != {"version", "id", "session", "result", "error"}
            or type(reply["version"]) is not int
            or reply["version"] != PROTOCOL_VERSION
            or type(reply["id"]) is not int
            or reply["id"] != self._sequence
        ):
            raise ClientProtocolError()
        if method == "session.attach":
            ref = reply["session"]
            if (
                not isinstance(ref, str)
                or len(ref) != 32
                or any(c not in "0123456789abcdef" for c in ref)
            ):
                raise ClientProtocolError()
            self.session_ref = ref
        elif self.session_ref is None or reply["session"] != self.session_ref:
            raise ClientProtocolError()
        error = reply["error"]
        if error is not None:
            if (
                not isinstance(error, str)
                or error not in HOST_ERRORS
                or reply["result"] is not None
            ):
                raise ClientProtocolError()
            from .errors import (
                AuthenticationRequired,
                LoginCancelled,
                LoginTimeout,
                RateLimited,
                RiskControl,
                WrongAccount,
            )

            kinds = {
                "AUTHENTICATION_REQUIRED": AuthenticationRequired,
                "LOGIN_CANCELLED": LoginCancelled,
                "LOGIN_TIMEOUT": LoginTimeout,
                "RATE_LIMITED": RateLimited,
                "RISK_CONTROL": RiskControl,
                "WRONG_ACCOUNT": WrongAccount,
                "CLIENT_SOURCE_UNAVAILABLE": ClientSourceUnavailable,
                "SOURCE_ADAPTER_MISSING": SourceAdapterMissing,
                "SOURCE_DATA_INVALID": SourceDataInvalid,
                "SOURCE_REQUEST_NOT_OBSERVED": SourceRequestNotObserved,
            }
            if error in kinds:
                raise kinds[error]()
            failure = OperationError()
            failure.code = error
            raise failure
        return reply["result"]

    async def attach(self):
        result = await self.call("session.attach")
        if not isinstance(result, dict) or set(result) != {"browser", "source_reader"}:
            raise ClientProtocolError()
        if result["browser"] != "client-built-in" or type(result["source_reader"]) is not bool:
            raise ClientProtocolError()
        self.source_reader = result["source_reader"]

    def require_source_reader(self):
        if not self.source_reader:
            raise SourceAdapterMissing()

    async def require_sources(self, game, sources):
        """Each game declares the sources required for this operation before any save."""
        requested = tuple(sources)
        if not self.source_reader:
            raise SourceAdapterMissing(requested)
        result = await self.call("source.check", {"game": game, "sources": list(requested)})
        if (
            not isinstance(result, dict)
            or set(result) != {"available"}
            or not isinstance(result["available"], list)
            or any(
                not isinstance(name, str) or name not in requested for name in result["available"]
            )
            or len(set(result["available"])) != len(result["available"])
        ):
            raise ClientProtocolError()
        missing = tuple(name for name in requested if name not in result["available"])
        if missing:
            raise ClientSourceUnavailable(missing)
