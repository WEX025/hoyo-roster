"""Authoritative persistence for Owner phase and context generation."""

from __future__ import annotations

import os
import sqlite3
import time

from .config import Settings
from .db import Database
from .errors import BrokerDatabaseError
from .models import OwnerPhase, OwnerRecord, StableCode
from .operational_events import (
    OperationalEvent,
    OperationalEventStore,
    OperationalEventType,
    diagnostic_hash,
)


class OwnerStateStore:
    """Own every worker_state phase and context generation transition."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        events: OperationalEventStore,
    ) -> None:
        self.settings = settings
        self.database = database
        self.events = events

    def heartbeat(
        self,
        worker_id: str,
        job_id: str | None = None,
        *,
        runtime_digest: str | None = None,
    ) -> None:
        current = time.time()
        try:
            with self.database.immediate() as connection:
                updated = connection.execute(
                    """
                    UPDATE worker_state
                    SET heartbeat_at = ?,
                        runtime_digest = COALESCE(?, runtime_digest)
                    WHERE singleton_id = 1
                      AND worker_id = ?
                      AND owner_phase IN ('STARTING', 'RUNNING')
                    """,
                    (current, runtime_digest, worker_id),
                )
                if updated.rowcount != 1:
                    raise BrokerDatabaseError("worker heartbeat lost its Owner authority")
                if job_id is not None:
                    job_updated = connection.execute(
                        """
                        UPDATE jobs
                        SET heartbeat_at = ?
                        WHERE job_id = ? AND state = 'RUNNING' AND worker_id = ?
                        """,
                        (current, job_id, worker_id),
                    )
                    if job_updated.rowcount != 1:
                        raise BrokerDatabaseError("job heartbeat lost its authority")
        except sqlite3.Error as error:
            raise BrokerDatabaseError("worker heartbeat failed") from error

    def begin_owner(self, worker_id: str, *, runtime_digest: str) -> None:
        current = time.time()
        try:
            with self.database.immediate() as connection:
                updated = connection.execute(
                    """
                    UPDATE worker_state
                    SET worker_id = ?, pid = ?, heartbeat_at = ?, runtime_digest = ?,
                        owner_phase = 'STARTING', browser_context_ready = 0,
                        context_generation = NULL
                    WHERE singleton_id = 1
                      AND worker_id IS NULL
                      AND owner_phase = 'STOPPED'
                    """,
                    (worker_id, os.getpid(), current, runtime_digest),
                )
                if updated.rowcount != 1:
                    raise BrokerDatabaseError("Owner startup state is not clean")
                self.events.record_in_transaction(
                    connection,
                    OperationalEvent(
                        occurred_at=current,
                        event_type=OperationalEventType.OWNER_STARTING,
                        game=None,
                        operation=None,
                        job_id=None,
                        stable_code=None,
                        diagnostic_hash=diagnostic_hash(
                            namespace="owner",
                            value=worker_id,
                        ),
                    ),
                )
        except sqlite3.Error as error:
            raise BrokerDatabaseError("Owner startup state could not be recorded") from error

    def mark_owner_running(
        self,
        worker_id: str,
        *,
        context_generation: str,
    ) -> None:
        if not context_generation:
            raise ValueError("Owner context generation is required")
        current = time.time()
        try:
            with self.database.immediate() as connection:
                updated = connection.execute(
                    """
                    UPDATE worker_state
                    SET owner_phase = 'RUNNING', browser_context_ready = 1,
                        context_generation = ?, heartbeat_at = ?
                    WHERE singleton_id = 1
                      AND worker_id = ?
                      AND owner_phase = 'STARTING'
                    """,
                    (context_generation, current, worker_id),
                )
                if updated.rowcount != 1:
                    raise BrokerDatabaseError("Owner RUNNING transition lost its authority")
                owner_hash = diagnostic_hash(namespace="owner", value=worker_id)
                context_hash = diagnostic_hash(
                    namespace="context",
                    value=context_generation,
                )
                self.events.record_in_transaction(
                    connection,
                    OperationalEvent(
                        occurred_at=current,
                        event_type=OperationalEventType.OWNER_READY,
                        game=None,
                        operation=None,
                        job_id=None,
                        stable_code=None,
                        diagnostic_hash=owner_hash,
                    ),
                )
                self.events.record_in_transaction(
                    connection,
                    OperationalEvent(
                        occurred_at=current,
                        event_type=OperationalEventType.CONTEXT_CREATED,
                        game=None,
                        operation=None,
                        job_id=None,
                        stable_code=None,
                        diagnostic_hash=context_hash,
                    ),
                )
        except sqlite3.Error as error:
            raise BrokerDatabaseError("Owner RUNNING transition failed") from error

    def mark_broken_in_transaction(
        self,
        connection: sqlite3.Connection,
        worker_id: str,
        *,
        current: float,
    ) -> None:
        row = connection.execute(
            """
            SELECT context_generation
            FROM worker_state
            WHERE singleton_id = 1 AND worker_id = ?
            """,
            (worker_id,),
        ).fetchone()
        if row is None:
            raise BrokerDatabaseError("Owner BROKEN transition lost its authority")
        context_generation = (
            str(row["context_generation"]) if row["context_generation"] is not None else None
        )
        updated = connection.execute(
            """
            UPDATE worker_state
            SET owner_phase = 'BROKEN', browser_context_ready = 0,
                context_generation = NULL, heartbeat_at = ?
            WHERE singleton_id = 1 AND worker_id = ?
            """,
            (current, worker_id),
        )
        if updated.rowcount != 1:
            raise BrokerDatabaseError("Owner BROKEN transition lost its authority")
        self.events.record_in_transaction(
            connection,
            OperationalEvent(
                occurred_at=current,
                event_type=OperationalEventType.OWNER_BROKEN,
                game=None,
                operation=None,
                job_id=None,
                stable_code=StableCode.OWNER_BROKEN,
                diagnostic_hash=diagnostic_hash(
                    namespace="owner",
                    value=worker_id,
                ),
            ),
        )
        if context_generation is not None:
            self.events.record_in_transaction(
                connection,
                OperationalEvent(
                    occurred_at=current,
                    event_type=OperationalEventType.CONTEXT_INVALIDATED,
                    game=None,
                    operation=None,
                    job_id=None,
                    stable_code=StableCode.OWNER_BROKEN,
                    diagnostic_hash=diagnostic_hash(
                        namespace="context",
                        value=context_generation,
                    ),
                ),
            )

    def mark_owner_broken(self, worker_id: str) -> None:
        try:
            with self.database.immediate() as connection:
                self.mark_broken_in_transaction(
                    connection,
                    worker_id,
                    current=time.time(),
                )
        except sqlite3.Error as error:
            raise BrokerDatabaseError("Owner BROKEN transition failed") from error

    def clear_worker(self, worker_id: str) -> None:
        try:
            with self.database.immediate() as connection:
                current = time.time()
                row = connection.execute(
                    """
                    SELECT owner_phase, context_generation
                    FROM worker_state
                    WHERE singleton_id = 1 AND worker_id = ?
                    """,
                    (worker_id,),
                ).fetchone()
                if row is None:
                    raise BrokerDatabaseError("Owner STOPPED transition lost its authority")
                old_phase = OwnerPhase(str(row["owner_phase"]))
                context_generation = (
                    str(row["context_generation"])
                    if row["context_generation"] is not None
                    else None
                )
                updated = connection.execute(
                    """
                    UPDATE worker_state
                    SET worker_id = NULL, pid = NULL, heartbeat_at = NULL,
                        runtime_digest = NULL, owner_phase = 'STOPPED',
                        browser_context_ready = 0, context_generation = NULL
                    WHERE singleton_id = 1
                      AND worker_id = ?
                      AND owner_phase IN ('STOPPING', 'BROKEN')
                    """,
                    (worker_id,),
                )
                if updated.rowcount != 1:
                    raise BrokerDatabaseError("Owner STOPPED transition lost its authority")
                if context_generation is not None:
                    self.events.record_in_transaction(
                        connection,
                        OperationalEvent(
                            occurred_at=current,
                            event_type=OperationalEventType.CONTEXT_INVALIDATED,
                            game=None,
                            operation=None,
                            job_id=None,
                            stable_code=None,
                            diagnostic_hash=diagnostic_hash(
                                namespace="context",
                                value=context_generation,
                            ),
                        ),
                    )
                owner_hash = diagnostic_hash(namespace="owner", value=worker_id)
                if old_phase is OwnerPhase.BROKEN:
                    self.events.record_in_transaction(
                        connection,
                        OperationalEvent(
                            occurred_at=current,
                            event_type=OperationalEventType.OWNER_RECOVERED,
                            game=None,
                            operation=None,
                            job_id=None,
                            stable_code=None,
                            diagnostic_hash=owner_hash,
                        ),
                    )
                self.events.record_in_transaction(
                    connection,
                    OperationalEvent(
                        occurred_at=current,
                        event_type=OperationalEventType.OWNER_STOPPED,
                        game=None,
                        operation=None,
                        job_id=None,
                        stable_code=None,
                        diagnostic_hash=owner_hash,
                    ),
                )
        except sqlite3.Error as error:
            raise BrokerDatabaseError("worker state cleanup failed") from error

    def worker_heartbeat(self) -> tuple[str | None, float | None]:
        try:
            with self.database.connection() as connection:
                row = connection.execute(
                    """
                    SELECT worker_id, heartbeat_at
                    FROM worker_state
                    WHERE singleton_id = 1
                    """
                ).fetchone()
            if row is None:
                raise BrokerDatabaseError("worker state row is missing")
            worker_id = row["worker_id"]
            heartbeat = row["heartbeat_at"]
            return (
                str(worker_id) if worker_id is not None else None,
                float(heartbeat) if heartbeat is not None else None,
            )
        except (sqlite3.Error, TypeError, ValueError) as error:
            if isinstance(error, BrokerDatabaseError):
                raise
            raise BrokerDatabaseError("worker state could not be read") from error

    def owner_record(self) -> OwnerRecord:
        try:
            with self.database.connection() as connection:
                row = connection.execute(
                    """
                    SELECT worker_id, pid, heartbeat_at, runtime_digest,
                           owner_phase, browser_context_ready, context_generation
                    FROM worker_state
                    WHERE singleton_id = 1
                    """
                ).fetchone()
            if row is None:
                raise BrokerDatabaseError("Owner state row is missing")
            return OwnerRecord(
                owner_id=(str(row["worker_id"]) if row["worker_id"] is not None else None),
                pid=int(row["pid"]) if row["pid"] is not None else None,
                heartbeat_at=(
                    float(row["heartbeat_at"]) if row["heartbeat_at"] is not None else None
                ),
                runtime_digest=(
                    str(row["runtime_digest"]) if row["runtime_digest"] is not None else None
                ),
                phase=OwnerPhase(str(row["owner_phase"])),
                browser_context_ready=bool(row["browser_context_ready"]),
                context_generation=(
                    str(row["context_generation"])
                    if row["context_generation"] is not None
                    else None
                ),
            )
        except (sqlite3.Error, TypeError, ValueError) as error:
            if isinstance(error, BrokerDatabaseError):
                raise
            raise BrokerDatabaseError("Owner state could not be read") from error

    def queue_counts(self) -> tuple[int, int]:
        try:
            with self.database.connection() as connection:
                row = connection.execute(
                    """
                    SELECT
                        SUM(CASE WHEN state = 'QUEUED' THEN 1 ELSE 0 END),
                        SUM(CASE WHEN state = 'RUNNING' THEN 1 ELSE 0 END)
                    FROM jobs
                    """
                ).fetchone()
            if row is None:
                raise BrokerDatabaseError("Owner queue counts are missing")
            return int(row[0] or 0), int(row[1] or 0)
        except (sqlite3.Error, TypeError, ValueError) as error:
            if isinstance(error, BrokerDatabaseError):
                raise
            raise BrokerDatabaseError("Owner queue counts could not be read") from error

    def commit_owner_stop_if_idle(self, worker_id: str) -> bool:
        """Commit STOPPING only while the caller holds protocol.lock."""

        try:
            with self.database.immediate() as connection:
                counts = connection.execute(
                    """
                    SELECT
                        SUM(CASE WHEN state = 'QUEUED' THEN 1 ELSE 0 END),
                        SUM(CASE WHEN state = 'RUNNING' THEN 1 ELSE 0 END)
                    FROM jobs
                    """
                ).fetchone()
                if counts is None:
                    raise BrokerDatabaseError("Owner queue counts are missing")
                if int(counts[0] or 0) or int(counts[1] or 0):
                    return False
                updated = connection.execute(
                    """
                    UPDATE worker_state
                    SET owner_phase = 'STOPPING', browser_context_ready = 0,
                        heartbeat_at = ?
                    WHERE singleton_id = 1
                      AND worker_id = ?
                      AND owner_phase = 'RUNNING'
                      AND browser_context_ready = 1
                    """,
                    (time.time(), worker_id),
                )
                if updated.rowcount != 1:
                    raise BrokerDatabaseError("Owner STOPPING transition lost its authority")
                self.events.record_in_transaction(
                    connection,
                    OperationalEvent(
                        occurred_at=time.time(),
                        event_type=OperationalEventType.OWNER_STOPPING,
                        game=None,
                        operation=None,
                        job_id=None,
                        stable_code=None,
                        diagnostic_hash=diagnostic_hash(
                            namespace="owner",
                            value=worker_id,
                        ),
                    ),
                )
                return True
        except (sqlite3.Error, TypeError, ValueError) as error:
            if isinstance(error, BrokerDatabaseError):
                raise
            raise BrokerDatabaseError("Owner STOPPING transition failed") from error
