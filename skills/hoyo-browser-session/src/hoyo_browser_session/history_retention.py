"""Bounded terminal Job retention without pruning durable obligations."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from enum import StrEnum

from .config import TERMINAL_JOB_HISTORY_LIMIT, Settings
from .db import Database
from .errors import BrokerDatabaseError
from .locks import FileLock, ensure_private_file
from .models import StableCode
from .operational_events import diagnostic_hash
from .worker_protocol import ProtocolInventory, classify_protocol_inventory

RETENTION_MAINTENANCE_FAILED = "RETENTION_MAINTENANCE_FAILED"


class RetentionMaintenanceStatus(StrEnum):
    PASS = "PASS"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class RetentionMaintenanceResult:
    status: RetentionMaintenanceStatus
    pruned_count: int
    stable_code: str | None = None
    diagnostic_hash: str | None = None

    def __post_init__(self) -> None:
        passed = self.status is RetentionMaintenanceStatus.PASS
        if self.pruned_count < 0:
            raise ValueError("retention maintenance prune count was invalid")
        if passed and (self.stable_code is not None or self.diagnostic_hash is not None):
            raise ValueError("retention maintenance result was inconsistent")
        if not passed and (
            self.stable_code != RETENTION_MAINTENANCE_FAILED or self.diagnostic_hash is None
        ):
            raise ValueError("retention maintenance result was inconsistent")

    @property
    def succeeded(self) -> bool:
        return self.status is RetentionMaintenanceStatus.PASS


@dataclass(frozen=True, slots=True)
class TerminalHistoryInspection:
    terminal_total: int
    terminal_limit: int
    active_count: int
    delivery_grace_protected: int
    recent_success_protected: int
    uncertain_protected: int
    protocol_terminal_protected: int
    invalid_terminal_protected: int
    protected_terminal_total: int
    eligible_terminal_count: int
    prunable_excess_count: int
    protocol_nonterminal_count: int
    protocol_unclassified_count: int
    prunable_job_ids: tuple[str, ...] = field(repr=False)

    @property
    def bounded(self) -> bool:
        return self.prunable_excess_count == 0


class HistoryRetention:
    """The sole authority for pruning terminal Job rows."""

    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.database = database

    def inspect_from_connection(
        self,
        connection: sqlite3.Connection,
        *,
        current: float,
        protocol_inventory: ProtocolInventory | None = None,
    ) -> TerminalHistoryInspection:
        """Classify terminal history with the same rules used by pruning."""

        inventory = (
            classify_protocol_inventory(self.settings, strict=False)
            if protocol_inventory is None
            else protocol_inventory
        )
        protocol_job_ids = inventory.safe_job_ids
        rows = connection.execute(
            """
            SELECT job_id, state, finished_at, result_code
            FROM jobs
            WHERE state IN ('SUCCEEDED', 'FAILED', 'BLOCKED', 'ABANDONED')
            ORDER BY finished_at DESC, job_id DESC
            """
        ).fetchall()
        active_row = connection.execute(
            """
            SELECT COUNT(*)
            FROM jobs
            WHERE state IN ('QUEUED', 'RUNNING')
            """
        ).fetchone()
        if active_row is None:
            raise BrokerDatabaseError("retention active Job status was unavailable")

        grace_boundary = current - self.settings.terminal_result_delivery_grace_seconds
        reuse_boundary = current - self.settings.recent_success_reuse_seconds
        delivery_grace_protected = 0
        recent_success_protected = 0
        uncertain_protected = 0
        protocol_terminal_protected = 0
        invalid_terminal_protected = 0
        protected_job_ids: set[str] = set()
        terminal_job_ids: set[str] = set()
        eligible_job_ids: list[str] = []
        for row in rows:
            job_id = str(row["job_id"])
            state = str(row["state"])
            terminal_job_ids.add(job_id)
            finished_at = float(row["finished_at"]) if row["finished_at"] is not None else None
            result_code = str(row["result_code"]) if row["result_code"] is not None else None
            invalid = finished_at is None or result_code is None
            delivery_grace = finished_at is not None and finished_at >= grace_boundary
            recent_success = (
                state == "SUCCEEDED" and finished_at is not None and finished_at >= reuse_boundary
            )
            uncertain = result_code == StableCode.PUBLISH_OUTCOME_UNKNOWN.value
            protocol_terminal = job_id in protocol_job_ids
            if invalid:
                invalid_terminal_protected += 1
            if delivery_grace:
                delivery_grace_protected += 1
            if recent_success:
                recent_success_protected += 1
            if uncertain:
                uncertain_protected += 1
            if protocol_terminal:
                protocol_terminal_protected += 1
            if invalid or delivery_grace or recent_success or uncertain or protocol_terminal:
                protected_job_ids.add(job_id)
            else:
                eligible_job_ids.append(job_id)

        prunable_job_ids = tuple(eligible_job_ids[TERMINAL_JOB_HISTORY_LIMIT:])
        return TerminalHistoryInspection(
            terminal_total=len(rows),
            terminal_limit=TERMINAL_JOB_HISTORY_LIMIT,
            active_count=int(active_row[0]),
            delivery_grace_protected=delivery_grace_protected,
            recent_success_protected=recent_success_protected,
            uncertain_protected=uncertain_protected,
            protocol_terminal_protected=protocol_terminal_protected,
            invalid_terminal_protected=invalid_terminal_protected,
            protected_terminal_total=len(protected_job_ids),
            eligible_terminal_count=len(eligible_job_ids),
            prunable_excess_count=len(prunable_job_ids),
            protocol_nonterminal_count=len(protocol_job_ids - terminal_job_ids),
            protocol_unclassified_count=inventory.unsafe_or_unclassified_count,
            prunable_job_ids=prunable_job_ids,
        )

    def prune(self, *, now: float | None = None) -> int:
        """Prune only terminal rows outside every active durability window."""

        current = time.time() if now is None else now
        ensure_private_file(self.settings.protocol_lock_file)
        protocol_lock = FileLock(self.settings.protocol_lock_file)
        protocol_lock.acquire(blocking=True)
        try:
            protocol_inventory = classify_protocol_inventory(
                self.settings,
                strict=True,
            )
            with self.database.immediate() as connection:
                inspection = self.inspect_from_connection(
                    connection,
                    current=current,
                    protocol_inventory=protocol_inventory,
                )
                for job_id in inspection.prunable_job_ids:
                    deleted = connection.execute(
                        """
                        DELETE FROM jobs
                        WHERE job_id = ?
                          AND state IN ('SUCCEEDED', 'FAILED', 'BLOCKED', 'ABANDONED')
                          AND result_code != 'PUBLISH_OUTCOME_UNKNOWN'
                        """,
                        (job_id,),
                    )
                    if deleted.rowcount != 1:
                        raise BrokerDatabaseError("terminal Job retention lost its authority")
                return len(inspection.prunable_job_ids)
        except (OSError, sqlite3.Error, TypeError, ValueError) as error:
            if isinstance(error, BrokerDatabaseError):
                raise
            raise BrokerDatabaseError("terminal Job retention failed") from error
        finally:
            protocol_lock.release()

    def maintain(self, *, now: float | None = None) -> RetentionMaintenanceResult:
        """Run one isolated maintenance attempt without changing core result semantics."""

        try:
            pruned_count = self.prune(now=now)
        except Exception as error:
            error_identity = f"{type(error).__module__}.{type(error).__qualname__}"
            return RetentionMaintenanceResult(
                status=RetentionMaintenanceStatus.FAILED,
                pruned_count=0,
                stable_code=RETENTION_MAINTENANCE_FAILED,
                diagnostic_hash=diagnostic_hash(
                    namespace="retention-maintenance",
                    value=error_identity,
                ),
            )
        return RetentionMaintenanceResult(
            status=RetentionMaintenanceStatus.PASS,
            pruned_count=pruned_count,
        )
