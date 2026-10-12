"""Bounded privacy-safe operational event authority."""

from __future__ import annotations

import hashlib
import math
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum

from .config import EVENT_HISTORY_LIMIT, SUPPORTED_GAMES, Settings
from .db import Database
from .errors import BrokerDatabaseError
from .models import Job, JobState, StableCode

_OPERATION_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class OperationalEventType(StrEnum):
    OWNER_STARTING = "OWNER_STARTING"
    OWNER_READY = "OWNER_READY"
    OWNER_STOPPING = "OWNER_STOPPING"
    OWNER_STOPPED = "OWNER_STOPPED"
    OWNER_BROKEN = "OWNER_BROKEN"
    OWNER_RECOVERED = "OWNER_RECOVERED"
    CONTEXT_CREATED = "CONTEXT_CREATED"
    CONTEXT_INVALIDATED = "CONTEXT_INVALIDATED"
    JOB_SUBMITTED = "JOB_SUBMITTED"
    JOB_COALESCED = "JOB_COALESCED"
    JOB_REUSED = "JOB_REUSED"
    JOB_CLAIMED = "JOB_CLAIMED"
    JOB_SUCCEEDED = "JOB_SUCCEEDED"
    JOB_FAILED = "JOB_FAILED"
    JOB_BLOCKED = "JOB_BLOCKED"
    JOB_ABANDONED = "JOB_ABANDONED"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"
    RISK_CONTROL = "RISK_CONTROL"
    PROFILE_BROKEN = "PROFILE_BROKEN"
    RUNTIME_MISMATCH = "RUNTIME_MISMATCH"
    PUBLISH_OUTCOME_UNKNOWN = "PUBLISH_OUTCOME_UNKNOWN"
    PUBLISH_RECONCILED = "PUBLISH_RECONCILED"
    PUBLISH_ACKNOWLEDGED = "PUBLISH_ACKNOWLEDGED"
    RETENTION_MAINTENANCE_FAILED = "RETENTION_MAINTENANCE_FAILED"
    RETENTION_MAINTENANCE_RECOVERED = "RETENTION_MAINTENANCE_RECOVERED"


@dataclass(frozen=True, slots=True)
class OperationalEvent:
    occurred_at: float
    event_type: OperationalEventType
    game: str | None
    operation: str | None
    job_id: str | None
    stable_code: StableCode | None
    diagnostic_hash: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "occurred_at": self.occurred_at,
            "event_type": self.event_type.value,
            "game": self.game,
            "operation": self.operation,
            "job_id": self.job_id,
            "stable_code": (self.stable_code.value if self.stable_code is not None else None),
            "diagnostic_hash": self.diagnostic_hash,
        }


def diagnostic_hash(*, namespace: str, value: str) -> str:
    """Return an irreversible domain-separated identifier."""

    if not namespace or not value:
        raise ValueError("diagnostic hash input was empty")
    return hashlib.sha256(f"{namespace}\0{value}".encode()).hexdigest()


def _validate_event(event: OperationalEvent) -> None:
    if (
        isinstance(event.occurred_at, bool)
        or not isinstance(event.occurred_at, int | float)
        or not math.isfinite(event.occurred_at)
        or event.occurred_at < 0
    ):
        raise ValueError("operational event time was invalid")
    if event.game is not None and event.game not in SUPPORTED_GAMES:
        raise ValueError("operational event game was invalid")
    if event.operation is not None and _OPERATION_RE.fullmatch(event.operation) is None:
        raise ValueError("operational event operation was invalid")
    if event.job_id is not None:
        try:
            canonical_job_id = str(uuid.UUID(event.job_id))
        except ValueError as error:
            raise ValueError("operational event Job ID was invalid") from error
        if canonical_job_id != event.job_id:
            raise ValueError("operational event Job ID was not canonical")
    if event.diagnostic_hash is not None and _HASH_RE.fullmatch(event.diagnostic_hash) is None:
        raise ValueError("operational event diagnostic hash was invalid")


def _terminal_event_types(
    state: JobState,
    stable_code: StableCode,
) -> tuple[OperationalEventType, ...]:
    primary = {
        JobState.SUCCEEDED: OperationalEventType.JOB_SUCCEEDED,
        JobState.FAILED: OperationalEventType.JOB_FAILED,
        JobState.BLOCKED: OperationalEventType.JOB_BLOCKED,
        JobState.ABANDONED: OperationalEventType.JOB_ABANDONED,
    }.get(state)
    if primary is None:
        raise ValueError("operational event requires a terminal Job state")
    special = {
        StableCode.AUTHENTICATION_REQUIRED: (OperationalEventType.AUTHENTICATION_REQUIRED),
        StableCode.LOGIN_TIMEOUT: OperationalEventType.AUTHENTICATION_REQUIRED,
        StableCode.WRONG_ACCOUNT: OperationalEventType.AUTHENTICATION_REQUIRED,
        StableCode.RATE_LIMITED: OperationalEventType.RATE_LIMITED,
        StableCode.RISK_CONTROL: OperationalEventType.RISK_CONTROL,
        StableCode.PROFILE_BROKEN: OperationalEventType.PROFILE_BROKEN,
        StableCode.OWNER_RUNTIME_RESTART_REQUIRED: (OperationalEventType.RUNTIME_MISMATCH),
        StableCode.PUBLISH_OUTCOME_UNKNOWN: (OperationalEventType.PUBLISH_OUTCOME_UNKNOWN),
    }.get(stable_code)
    return (primary,) if special is None else (primary, special)


class OperationalEventStore:
    """The sole writer and reader for operational event history."""

    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.database = database

    def record(
        self,
        event_type: OperationalEventType,
        *,
        occurred_at: float | None = None,
        game: str | None = None,
        operation: str | None = None,
        job_id: str | None = None,
        stable_code: StableCode | None = None,
        diagnostic_hash_value: str | None = None,
    ) -> None:
        event = OperationalEvent(
            occurred_at=time.time() if occurred_at is None else occurred_at,
            event_type=event_type,
            game=game,
            operation=operation,
            job_id=job_id,
            stable_code=stable_code,
            diagnostic_hash=diagnostic_hash_value,
        )
        try:
            with self.database.immediate() as connection:
                self.record_in_transaction(connection, event)
        except sqlite3.Error as error:
            raise BrokerDatabaseError("operational event could not be recorded") from error

    def record_in_transaction(
        self,
        connection: sqlite3.Connection,
        event: OperationalEvent,
    ) -> None:
        _validate_event(event)
        connection.execute(
            """
            INSERT INTO operational_events(
                occurred_at, event_type, game, operation, job_id,
                stable_code, diagnostic_hash
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.occurred_at,
                event.event_type.value,
                event.game,
                event.operation,
                event.job_id,
                event.stable_code.value if event.stable_code is not None else None,
                event.diagnostic_hash,
            ),
        )
        connection.execute(
            """
            DELETE FROM operational_events
            WHERE event_id IN (
                SELECT event_id
                FROM operational_events
                ORDER BY occurred_at DESC, event_id DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (EVENT_HISTORY_LIMIT,),
        )

    def record_job_event_in_transaction(
        self,
        connection: sqlite3.Connection,
        event_type: OperationalEventType,
        job: Job,
        *,
        occurred_at: float,
        stable_code: StableCode | None = None,
    ) -> None:
        self.record_in_transaction(
            connection,
            OperationalEvent(
                occurred_at=occurred_at,
                event_type=event_type,
                game=job.game,
                operation=job.operation,
                job_id=job.job_id,
                stable_code=stable_code,
                diagnostic_hash=None,
            ),
        )

    def record_terminal_in_transaction(
        self,
        connection: sqlite3.Connection,
        job: Job,
        *,
        state: JobState,
        stable_code: StableCode,
        occurred_at: float,
    ) -> None:
        for event_type in _terminal_event_types(state, stable_code):
            self.record_job_event_in_transaction(
                connection,
                event_type,
                job,
                occurred_at=occurred_at,
                stable_code=stable_code,
            )

    @staticmethod
    def recent_from_connection(
        connection: sqlite3.Connection,
        *,
        limit: int,
    ) -> list[OperationalEvent]:
        if limit < 0 or limit > 100:
            raise ValueError("operational event inspection limit was invalid")
        rows = connection.execute(
            """
            SELECT occurred_at, event_type, game, operation, job_id,
                   stable_code, diagnostic_hash
            FROM operational_events
            ORDER BY occurred_at DESC, event_id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        events: list[OperationalEvent] = []
        for row in rows:
            event = OperationalEvent(
                occurred_at=float(row["occurred_at"]),
                event_type=OperationalEventType(str(row["event_type"])),
                game=str(row["game"]) if row["game"] is not None else None,
                operation=(str(row["operation"]) if row["operation"] is not None else None),
                job_id=str(row["job_id"]) if row["job_id"] is not None else None,
                stable_code=(
                    StableCode(str(row["stable_code"])) if row["stable_code"] is not None else None
                ),
                diagnostic_hash=(
                    str(row["diagnostic_hash"]) if row["diagnostic_hash"] is not None else None
                ),
            )
            _validate_event(event)
            events.append(event)
        return events
