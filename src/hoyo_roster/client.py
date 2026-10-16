"""A bounded, correlated bridge to the AI client's live browser host; no browser driver."""

from __future__ import annotations

import asyncio
import os
import threading
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
        self.pending = {}
        self.loop = None
        self.failed = False

    def _receive(self, raw):
        try:
            if not raw or len(raw) > MAX_FRAME_BYTES or not raw.endswith(b"\n"):
                raise ClientProtocolError()
            reply = loads_json(raw.decode("utf-8"))
            request_id = reply.get("id") if isinstance(reply, dict) else None
            if type(request_id) is not int or request_id not in self.pending:
                raise ClientProtocolError()
            self.pending.pop(request_id).set_result(reply)
        except Exception:
            self.failed = True
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ClientProtocolError())
            self.pending.clear()

    def _read(self):
        # This daemon only reads the private pipe; browser work remains in active pumps.
        try:
            descriptor = self.reader.fileno()
        except (AttributeError, OSError):
            descriptor = None
        buffer = b""
        while True:
            try:
                if descriptor is None:
                    raw = self.reader.readline(MAX_FRAME_BYTES + 1)
                else:
                    # Do not hold BufferedReader's lock while awaiting private-pipe
                    # input: a daemon holding it would block interpreter shutdown.
                    while b"\n" not in buffer and len(buffer) <= MAX_FRAME_BYTES:
                        chunk = os.read(descriptor, min(65536, MAX_FRAME_BYTES + 1 - len(buffer)))
                        if not chunk:
                            break
                        buffer += chunk
                    if b"\n" in buffer:
                        raw, buffer = buffer.split(b"\n", 1)
                        raw += b"\n"
                    else:
                        raw, buffer = buffer, b""
            except Exception:
                raw = b""
            try:
                self.loop.call_soon_threadsafe(self._receive, raw)
            except RuntimeError:
                return
            if not raw:
                return

    async def exchange(self, message):
        encoded = json_bytes(message, compact=True)
        if len(encoded) > MAX_FRAME_BYTES:
            raise ClientProtocolError()
        if self.failed or message["id"] in self.pending or len(self.pending) >= 2:
            raise ClientProtocolError()
        future = asyncio.get_running_loop().create_future()
        self.pending[message["id"]] = future
        self.writer.write(encoded.rstrip(b"\n") + b"\n")
        self.writer.flush()
        if self.loop is None:
            self.loop = asyncio.get_running_loop()
            threading.Thread(target=self._read, daemon=True).start()
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            future.add_done_callback(lambda f: None if f.cancelled() else f.exception())
            raise

    async def drain(self):
        if self.pending:
            await asyncio.gather(*tuple(self.pending.values()), return_exceptions=True)


@dataclass(repr=False)
class ClientSession:
    port: ClientPort = field(repr=False)
    session_ref: str | None = field(default=None, init=False, repr=False)
    source_reader: bool = field(default=False, init=False)
    page_reader: bool = field(default=False, init=False)
    _sequence: int = field(default=0, init=False)

    async def call(self, method: str, params: dict | None = None):
        self._sequence += 1
        request_id = self._sequence
        message = {
            "version": PROTOCOL_VERSION,
            "id": request_id,
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
            or reply["id"] != request_id
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
        if not isinstance(result, dict) or set(result) not in (
            {"browser", "source_reader"},
            {"browser", "source_reader", "page_reader"},
        ):
            raise ClientProtocolError()
        if result["browser"] != "client-built-in" or type(result["source_reader"]) is not bool:
            raise ClientProtocolError()
        self.source_reader = result["source_reader"]
        if type(result.get("page_reader", False)) is not bool:
            raise ClientProtocolError()
        self.page_reader = result.get("page_reader", False)
        if self.page_reader and not self.source_reader:
            raise ClientProtocolError()

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
