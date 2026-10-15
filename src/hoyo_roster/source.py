"""Read exact source JSON and origin proof through the attached client."""

from urllib.parse import urlsplit

from .client import ClientSession
from .errors import (
    ClientProtocolError,
    SourceDataInvalid,
    AuthenticationRequired,
    RateLimited,
    RiskControl,
)
from .jsonio import loads_json


class ClientSource:
    def __init__(self, session, game):
        self.session = session
        self.game = game
        self.request_count = 0

    async def read(self, spec, *, params=None, body=None):
        client: ClientSession = self.session.client
        client.require_source_reader()
        await self.session.gate.reserve(requested_min_interval=2.0)
        result = await client.call(
            "source.read",
            {
                "game": self.game,
                "source": spec.name,
                "params": params or {},
                "body": body,
            },
        )
        self.request_count += 1
        if (
            not isinstance(result, dict)
            or set(result) != {"json", "evidence", "origin"}
            or not isinstance(result["json"], str)
        ):
            raise ClientProtocolError()
        origin = result["origin"]
        if not isinstance(origin, dict) or set(origin) != {"url", "method", "status", "redirected"}:
            raise SourceDataInvalid()
        try:
            actual = urlsplit(origin["url"])
        except (ValueError, TypeError, AttributeError):
            raise SourceDataInvalid() from None
        if (
            actual.scheme != "https"
            or actual.netloc != spec.host
            or actual.path != spec.path
            or actual.fragment
            or origin["method"] != spec.method
            or origin["redirected"] is not False
            or type(origin["status"]) is not int
        ):
            raise SourceDataInvalid()
        if origin["status"] == 429:
            raise RateLimited()
        if not 200 <= origin["status"] < 300:
            raise SourceDataInvalid()
        try:
            payload = loads_json(result["json"])
        except ValueError:
            raise SourceDataInvalid() from None
        if not isinstance(payload, dict) or type(payload.get("retcode")) is not int:
            raise SourceDataInvalid()
        code = payload["retcode"]
        if code in {-100, -101, 10001}:
            raise AuthenticationRequired()
        if code in {1034, 10035, 5003, 10041}:
            raise RiskControl()
        if code == 429:
            raise RateLimited()
        if code != 0 or not isinstance(payload.get("data"), dict):
            raise SourceDataInvalid()
        return payload["data"], result["evidence"]
