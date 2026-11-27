"""Honest rendered-page observations; these are never API response envelopes."""

from urllib.parse import urlsplit
import json
import time

from .errors import ClientProtocolError, SourceDataInvalid
from .jsonio import loads_json

PAGE_SOURCES = ("page:identity", "page:roster", "page:detail")


class PageSource:
    def __init__(self, session, game, url, detail_surface, *, fragments=None):
        self.session, self.game, self.url = session, game, url
        self.surfaces = {
            "identity": "role-selector",
            "roster": "owned-selector",
            "detail": detail_surface,
        }
        self.fragments = tuple(fragments) if fragments is not None else (urlsplit(url).fragment,)
        self.sequence = 0

    async def read(self, operation, params=None, *, recheck=False):
        generation = self.session.browser_generation
        self.sequence += 1
        sequence = self.sequence
        await self.session.gate.reserve(
            requested_min_interval=2.0, page_game=None if recheck else self.game
        )
        print(
            "PAGE_TIMING="
            + json.dumps(
                {
                    "game": self.game,
                    "request": sequence,
                    "operation": operation,
                    "phase": "gate",
                    **getattr(self.session.gate, "last_reservation", {}),
                }
            ),
            flush=True,
        )
        print(f"PAGE_SOURCE={self.game}:{operation}", flush=True)
        started = time.monotonic()
        try:
            result = await self.session.call_source(
                "source.read",
                {
                    "game": self.game,
                    "source": "page:" + operation,
                    **({"recheck": True} if recheck else {}),
                    "params": params or {},
                    "body": None,
                },
                generation=generation,
            )
        finally:
            print(
                "PAGE_TIMING="
                + json.dumps(
                    {
                        "game": self.game,
                        "request": sequence,
                        "operation": operation,
                        "phase": "source_roundtrip",
                        "duration_ms": (time.monotonic() - started) * 1000,
                    }
                ),
                flush=True,
            )
        if not isinstance(result, dict) or set(result) != {"json", "origin", "evidence"}:
            raise ClientProtocolError()
        origin, evidence = result["origin"], result["evidence"]
        if not isinstance(origin, dict) or set(origin) != {"kind", "game", "url"}:
            raise SourceDataInvalid()
        try:
            actual, expected = urlsplit(origin["url"]), urlsplit(self.url)
        except (ValueError, TypeError, AttributeError):
            raise SourceDataInvalid() from None
        if (
            origin["kind"] != "official-page"
            or origin["game"] != self.game
            or (actual.scheme, actual.netloc, actual.path, actual.query)
            != (expected.scheme, expected.netloc, expected.path, expected.query)
            or actual.fragment not in self.fragments
            or actual.scheme != "https"
            or not isinstance(evidence, dict)
            or set(evidence) != {"surface", "generation"}
            or evidence["surface"] != self.surfaces[operation]
            or type(evidence["generation"]) is not int
            or evidence["generation"] != generation
            or not isinstance(result["json"], str)
        ):
            raise SourceDataInvalid()
        try:
            value = loads_json(result["json"])
        except ValueError:
            raise SourceDataInvalid() from None
        if not isinstance(value, dict):
            raise SourceDataInvalid()
        return value
