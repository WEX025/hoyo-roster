"""Persist throttling under the shared client-session metadata lock."""

import asyncio
import math
import time
from .config import Settings
from .errors import ConfigurationError, RequestScheduleInvalid
from .storage.common import atomic_write_metadata, load_metadata


class RequestGate:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = settings.root / "request-state.json"
        self._reservation_lock = asyncio.Lock()
        self.cross_game_interval_ms = 2000
        self._last_page_game = None
        self._own_deadline = None

    async def reserve(self, *, requested_min_interval: float, page_game=None) -> float:
        queued = time.monotonic()
        async with self._reservation_lock:
            queue_wait = (time.monotonic() - queued) * 1000
            scheduled = await self._reserve(requested_min_interval, page_game)
            self.last_reservation["queue_wait_ms"] = queue_wait
            self.last_reservation["total_ms"] += queue_wait
            return scheduled

    async def _reserve(self, requested_min_interval, page_game) -> float:
        started = time.monotonic()
        if not math.isfinite(requested_min_interval) or requested_min_interval < 2:
            raise ConfigurationError("Request interval must be at least two seconds")
        now = time.time()
        state = (
            load_metadata(self.path, private=True) if self.path.exists() else {"next_allowed_at": 0}
        )
        if not isinstance(state, dict) or set(state) != {"next_allowed_at"}:
            raise ConfigurationError("Request state invalid")
        next_at = state["next_allowed_at"]
        if (
            not isinstance(next_at, (int, float))
            or isinstance(next_at, bool)
            or not math.isfinite(next_at)
        ):
            raise ConfigurationError("Request state invalid")
        # The persisted deadline always protects older clients, a new process,
        # and API reads. Only this live owner can use its previous page reservation.
        if (
            self.cross_game_interval_ms in (1000, 1500)
            and requested_min_interval == 2
            and page_game in ("genshin", "zzz")
            and self._last_page_game in ("genshin", "zzz")
            and page_game != self._last_page_game
            and self._own_deadline == next_at
        ):
            next_at -= 2 - self.cross_game_interval_ms / 1000
        scheduled = max(now, next_at)
        wait = scheduled - now
        if not math.isfinite(now) or wait > 30:
            # A future/corrupt clock reservation is not permission to reset throttling.
            # Reject before changing its evidence or waiting outside the operation budget.
            raise RequestScheduleInvalid("Persisted request schedule exceeds the wait budget")
        persist_started = time.monotonic()
        atomic_write_metadata(self.path, {"next_allowed_at": scheduled + requested_min_interval})
        self._own_deadline = scheduled + requested_min_interval
        self._last_page_game = page_game if requested_min_interval == 2 else None
        persisted = time.monotonic()
        await asyncio.sleep(wait)
        self.last_reservation = {
            "planned_wait_ms": wait * 1000,
            "actual_wait_ms": (time.monotonic() - persisted) * 1000,
            "persist_ms": (persisted - persist_started) * 1000,
            "total_ms": (time.monotonic() - started) * 1000,
        }
        return scheduled
