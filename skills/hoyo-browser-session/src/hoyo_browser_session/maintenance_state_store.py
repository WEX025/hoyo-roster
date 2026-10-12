"""Durable singleton authority for current maintenance health."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from enum import StrEnum

from .db import Database
from .errors import BrokerDatabaseError
from .history_retention import RetentionMaintenanceResult
from .operational_events import (
    OperationalEvent,
    OperationalEventStore,
    OperationalEventType,
)


class RetentionCurrentState(StrEnum):
    HEALTHY = "HEALTHY"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class RetentionCurrentRecord:
    state: RetentionCurrentState
    diagnostic_hash: str | None
    updated_at: float


class MaintenanceStateStore:
    """The sole writer for current retention maintenance state."""

    def __init__(
        self,
        database: Database,
        events: OperationalEventStore,
    ) -> None:
        self.database = database
        self.events = events

    def observe(
        self,
        result: RetentionMaintenanceResult,
        *,
        now: float | None = None,
    ) -> bool:
        """Persist current state and append only transition evidence."""

        current = time.time() if now is None else now
        target_state = (
            RetentionCurrentState.HEALTHY if result.succeeded else RetentionCurrentState.FAILED
        )
        target_diagnostic = None if result.succeeded else result.diagnostic_hash
        if target_state is RetentionCurrentState.FAILED and target_diagnostic is None:
            raise ValueError("retention failure diagnostic was unavailable")
        try:
            with self.database.immediate() as connection:
                row = connection.execute(
                    """
                    SELECT retention_state, retention_diagnostic_hash
                    FROM maintenance_state
                    WHERE singleton_id = 1
                    """
                ).fetchone()
                if row is None:
                    raise BrokerDatabaseError("retention current state was unavailable")
                previous_state = RetentionCurrentState(str(row["retention_state"]))
                previous_diagnostic = (
                    str(row["retention_diagnostic_hash"])
                    if row["retention_diagnostic_hash"] is not None
                    else None
                )
                transition = previous_state is not target_state
                diagnostic_changed = (
                    target_state is RetentionCurrentState.FAILED
                    and previous_diagnostic != target_diagnostic
                )
                updated = connection.execute(
                    """
                    UPDATE maintenance_state
                    SET retention_state = ?, retention_diagnostic_hash = ?,
                        updated_at = ?
                    WHERE singleton_id = 1
                    """,
                    (target_state.value, target_diagnostic, current),
                )
                if updated.rowcount != 1:
                    raise BrokerDatabaseError("retention current state update lost its authority")
                if transition or diagnostic_changed:
                    self.events.record_in_transaction(
                        connection,
                        OperationalEvent(
                            occurred_at=current,
                            event_type=(
                                OperationalEventType.RETENTION_MAINTENANCE_RECOVERED
                                if target_state is RetentionCurrentState.HEALTHY
                                else OperationalEventType.RETENTION_MAINTENANCE_FAILED
                            ),
                            game=None,
                            operation=None,
                            job_id=None,
                            stable_code=None,
                            diagnostic_hash=target_diagnostic,
                        ),
                    )
                return transition or diagnostic_changed
        except sqlite3.Error as error:
            raise BrokerDatabaseError("retention current state could not be persisted") from error

    @staticmethod
    def read_from_connection(
        connection: sqlite3.Connection,
    ) -> RetentionCurrentRecord:
        row = connection.execute(
            """
            SELECT retention_state, retention_diagnostic_hash, updated_at
            FROM maintenance_state
            WHERE singleton_id = 1
            """
        ).fetchone()
        if row is None:
            raise BrokerDatabaseError("retention current state was unavailable")
        state = RetentionCurrentState(str(row["retention_state"]))
        diagnostic = (
            str(row["retention_diagnostic_hash"])
            if row["retention_diagnostic_hash"] is not None
            else None
        )
        if (state is RetentionCurrentState.HEALTHY) != (diagnostic is None):
            raise BrokerDatabaseError("retention current state was inconsistent")
        return RetentionCurrentRecord(
            state=state,
            diagnostic_hash=diagnostic,
            updated_at=float(row["updated_at"]),
        )
