"""Persist throttling in the identity scope already protected by the workspace lock."""

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

    async def reserve(self, *, requested_min_interval: float) -> float:
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
        scheduled = max(now, next_at)
        wait = scheduled - now
        if not math.isfinite(now) or wait > 30:
            # A future/corrupt clock reservation is not permission to reset throttling.
            # Reject before changing its evidence or waiting outside the operation budget.
            raise RequestScheduleInvalid("Persisted request schedule exceeds the wait budget")
        atomic_write_metadata(self.path, {"next_allowed_at": scheduled + requested_min_interval})
        await asyncio.sleep(wait)
        return scheduled
