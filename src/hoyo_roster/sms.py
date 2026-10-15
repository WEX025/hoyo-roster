"""Passive official SMS receipt observation; never read/send credentials or retry."""

import asyncio
from time import monotonic
from urllib.parse import urlparse

from .errors import (
    RateLimited,
    RiskControl,
    SmsReceiptUnknown,
    SmsRequestRejected,
    SmsDuplicateRequest,
)
from .progress import emit

SMS_PATH = "/account/ma-cn-verifier/verifier/createLoginCaptcha"


def sms_request(request) -> bool:
    parsed = urlparse(request.url)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "passport-api.mihoyo.com"
        and parsed.port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and parsed.path == SMS_PATH
        and request.method == "POST"
    )


class SmsReceiptWatch:
    """Watch the one person-initiated request across SDK frames, with bounded cleanup."""

    def __init__(self, context):
        self.context = context
        self.request = None
        self.started = None
        self.task = None
        self.error = None
        self.state = None

    def start(self) -> None:
        self.context.on("request", self._request)
        self.context.on("response", self._response)
        self.context.on("requestfailed", self._failed)

    def _record_error(self, error) -> None:
        def priority(value):
            if isinstance(value, (RiskControl, RateLimited)):
                return 3
            if isinstance(value, SmsRequestRejected):
                return 2
            return 1 if value is not None else 0

        if priority(error) > priority(self.error):
            self.error = error.with_traceback(None)

    def _request(self, request) -> None:
        if not sms_request(request):
            return
        if self.request is not None:
            self._record_error(SmsDuplicateRequest("Repeated official SMS request observed"))
            return
        self.request = request
        self.started = monotonic()
        emit("SMS_REQUESTING")

    def _response(self, response) -> None:
        if response.request is self.request and self.request is not None and self.task is None:
            self.task = asyncio.create_task(self._read_receipt(response))

    def _failed(self, request) -> None:
        if request is self.request:
            self._record_error(SmsReceiptUnknown("Official SMS request transport failed"))

    async def _read_receipt(self, response) -> None:
        try:
            async with asyncio.timeout(5):
                if response.status == 429:
                    raise RateLimited("Official SMS request rate limited")
                if response.status != 200:
                    raise SmsReceiptUnknown("Official SMS response HTTP failure")
                payload = await response.json()
                if not isinstance(payload, dict) or type(payload.get("retcode")) is not int:
                    raise SmsReceiptUnknown("Official SMS receipt malformed")
                if payload["retcode"] in {1034, 10035, 5003, 10041}:
                    raise RiskControl("Official SMS receipt reported risk control")
                if payload["retcode"] != 0:
                    raise SmsRequestRejected("Official SMS request rejected")
                data = payload.get("data")
                if (
                    not isinstance(data, dict)
                    or type(data.get("sent_new")) is not bool
                    or type(data.get("countdown")) is not int
                    or data["countdown"] < 0
                ):
                    raise SmsReceiptUnknown("Official SMS receipt incomplete")
                self.state = "SMS_ACCEPTED" if data["sent_new"] else "SMS_COOLDOWN"
                emit(self.state)
        except asyncio.CancelledError:
            raise
        except (
            RateLimited,
            RiskControl,
            SmsReceiptUnknown,
            SmsRequestRejected,
            SmsDuplicateRequest,
        ) as error:
            self._record_error(error)
        except Exception:
            self._record_error(SmsReceiptUnknown("Official SMS receipt unavailable"))

    def check_protection(self) -> None:
        if isinstance(self.error, (RiskControl, RateLimited)):
            raise self.error

    def check(self) -> None:
        # CAPTCHA disappearance/Binding success never overrides an observed SMS error.
        if self.error is not None:
            if isinstance(self.error, SmsReceiptUnknown):
                emit("SMS_RECEIPT_UNKNOWN")
            raise self.error
        if self.started is not None and self.state is None and monotonic() - self.started >= 30:
            emit("SMS_RECEIPT_UNKNOWN")
            raise SmsReceiptUnknown("Official SMS receipt deadline exceeded")

    async def finish(self, *, deadline: float) -> None:
        """Resolve an observed request before claiming login success; never send/retry."""
        while True:
            self.check()
            if self.request is None or self.state is not None:
                return
            remaining = min(deadline, self.started + 30) - monotonic()
            if remaining <= 0:
                self._record_error(SmsReceiptUnknown("Official SMS receipt remained unresolved"))
                self.check()
            await asyncio.sleep(min(0.05, remaining))

    async def close(self) -> None:
        for event, handler in (
            ("request", self._request),
            ("response", self._response),
            ("requestfailed", self._failed),
        ):
            self.context.remove_listener(event, handler)
        if self.task is not None:
            if not self.task.done():
                self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.request = None
