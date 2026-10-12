"""Cross-process atomic reservation of shared request slots."""

from __future__ import annotations

import asyncio
import math
import sqlite3
import time

from .config import ACCOUNT_SCOPE, Settings
from .db import Database
from .errors import BrokerDatabaseError, ConfigurationError


class RequestGate:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.database = Database(settings)
        self.database.initialize()

    async def reserve(
        self,
        *,
        account_scope: str = ACCOUNT_SCOPE,
        requested_min_interval: float,
    ) -> float:
        if account_scope != ACCOUNT_SCOPE:
            raise ConfigurationError("only the primary request gate is available")
        if not math.isfinite(requested_min_interval) or requested_min_interval < 0:
            raise ConfigurationError("requested request interval is invalid")
        now = time.time()
        effective_interval = max(
            self.settings.shared_request_min_interval_seconds,
            requested_min_interval,
        )
        try:
            with self.database.immediate() as connection:
                row = connection.execute(
                    "SELECT next_allowed_at FROM rate_state WHERE scope_key = ?",
                    (account_scope,),
                ).fetchone()
                next_allowed_at = float(row[0]) if row is not None else 0.0
                scheduled_at = max(now, next_allowed_at)
                new_next_allowed_at = scheduled_at + effective_interval
                connection.execute(
                    """
                    INSERT INTO rate_state(scope_key, next_allowed_at, updated_at)
                    VALUES(?, ?, ?)
                    ON CONFLICT(scope_key) DO UPDATE SET
                        next_allowed_at = excluded.next_allowed_at,
                        updated_at = excluded.updated_at
                    """,
                    (account_scope, new_next_allowed_at, now),
                )
        except (sqlite3.Error, TypeError, ValueError) as error:
            raise BrokerDatabaseError("request slot reservation failed") from error
        delay = scheduled_at - time.time()
        if delay > 0:
            await asyncio.sleep(delay)
        return scheduled_at
