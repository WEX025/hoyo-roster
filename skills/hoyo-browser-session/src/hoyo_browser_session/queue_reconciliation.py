"""Uncertain publication, late-result, and crash reconciliation authority."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import time
import uuid

from .config import Settings
from .db import Database
from .errors import (
    ActiveOwnerAuthority,
    BrokerDatabaseError,
    BrokerError,
    ProtocolStateError,
    UncertainJobAcknowledgementRejected,
)
from .execution_identity import ExecutionIdentityAuthority
from .locks import FileLock, ensure_private_file, is_lock_held
from .models import (
    Job,
    JobResult,
    JobState,
    StableCode,
    SubmitDisposition,
)
from .queue_completion import CompletionAuthority
from .worker_protocol import (
    ResultFileIdentityMismatch,
    ResultFileInvalid,
    ResultFileMissing,
    ResultFileUnsafe,
    cleanup_job_protocol_files,
    executor_result_from_job_result,
    read_worker_result,
    result_paths,
    validate_job_protocol_files,
)

_UNKNOWN_RESULT_CODE = StableCode.PUBLISH_OUTCOME_UNKNOWN.value
_ACKNOWLEDGED_RESULT_CODE = (
    StableCode.PUBLISH_OUTCOME_UNKNOWN_ACKNOWLEDGED.value
)
_LATE_RESULT_MISSING = "LATE_RESULT_MISSING"
_LATE_RESULT_AVAILABLE = "LATE_RESULT_AVAILABLE"
_LATE_RESULT_INVALID = "LATE_RESULT_INVALID"
_LATE_RESULT_UNSAFE = "LATE_RESULT_UNSAFE"
_LATE_RESULT_IDENTITY_MISMATCH = "LATE_RESULT_IDENTITY_MISMATCH"


class ReconciliationAuthority:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        identity: ExecutionIdentityAuthority,
        completion: CompletionAuthority,
    ) -> None:
        self.settings = settings
        self.database = database
        self.identity = identity
        self.completion = completion

    def late_result(self, job: Job) -> tuple[str, JobResult | None]:
        try:
            result = read_worker_result(self.settings, job)
        except ResultFileMissing:
            return _LATE_RESULT_MISSING, None
        except ResultFileUnsafe:
            return _LATE_RESULT_UNSAFE, None
        except ResultFileIdentityMismatch:
            return _LATE_RESULT_IDENTITY_MISMATCH, None
        except ResultFileInvalid:
            return _LATE_RESULT_INVALID, None
        return _LATE_RESULT_AVAILABLE, result

    def reconcile_uncertain_results(
        self,
        *,
        job_id: str | None = None,
        now: float | None = None,
    ) -> dict[str, int]:
        """Apply valid late results without ever executing the original Job."""

        ensure_private_file(self.settings.protocol_lock_file)
        protocol_lock = FileLock(self.settings.protocol_lock_file)
        protocol_lock.acquire(blocking=True)
        try:
            return self.reconcile_uncertain_results_locked(
                job_id=job_id,
                now=now,
            )
        finally:
            protocol_lock.release()

    def reconcile_uncertain_results_locked(
        self,
        *,
        job_id: str | None = None,
        now: float | None = None,
    ) -> dict[str, int]:
        # Lock order is protocol.lock -> SQLite. No browser, network, or
        # Publisher operation is permitted while protocol.lock is held.
        current = time.time() if now is None else now
        summary = {
            "recovered": 0,
            "missing": 0,
            "invalid": 0,
            "unsafe": 0,
            "identity_mismatch": 0,
            "cleanup_pending": 0,
        }
        query = """
            SELECT *
            FROM jobs
            WHERE state = 'FAILED'
              AND result_code = 'PUBLISH_OUTCOME_UNKNOWN'
        """
        values: tuple[object, ...] = ()
        if job_id is not None:
            query += " AND job_id = ?"
            values = (job_id,)
        query += " ORDER BY created_at, job_id"
        try:
            with self.database.connection() as connection:
                rows = connection.execute(query, values).fetchall()
            for row in rows:
                job = self.completion.row_to_job(row)
                diagnostic, persisted = self.late_result(job)
                if persisted is None:
                    counter = {
                        _LATE_RESULT_MISSING: "missing",
                        _LATE_RESULT_INVALID: "invalid",
                        _LATE_RESULT_UNSAFE: "unsafe",
                        _LATE_RESULT_IDENTITY_MISMATCH: "identity_mismatch",
                    }[diagnostic]
                    summary[counter] += 1
                    continue
                self.completion.persist_uncertain_completion(
                    job,
                    persisted,
                    state_updated_at=current,
                )
                summary["recovered"] += 1
                try:
                    cleanup_job_protocol_files(self.settings, job.job_id)
                except (OSError, ValueError, BrokerError):
                    summary["cleanup_pending"] += 1
            return summary
        except (sqlite3.Error, ValueError, TypeError) as error:
            if isinstance(error, BrokerDatabaseError):
                raise
            raise BrokerDatabaseError(
                "uncertain result reconciliation failed"
            ) from error

    def reconcile_stale_running(
        self,
        worker_id: str,
        *,
        now: float | None = None,
    ) -> dict[str, int]:
        current = time.time() if now is None else now
        stale_before = current - self.settings.worker_stale_seconds
        ensure_private_file(self.settings.protocol_lock_file)
        protocol_lock = FileLock(self.settings.protocol_lock_file)
        protocol_lock.acquire(blocking=True)
        try:
            try:
                with self.database.connection() as connection:
                    rows = connection.execute(
                        """
                        SELECT *
                        FROM jobs
                        WHERE state = 'RUNNING'
                          AND (heartbeat_at IS NULL OR heartbeat_at < ?)
                        ORDER BY created_at, job_id
                        """,
                        (stale_before,),
                    ).fetchall()
                summary = {"recovered": 0, "abandoned": 0, "uncertain": 0}
                for row in rows:
                    job = self.completion.row_to_job(row)
                    try:
                        persisted = read_worker_result(self.settings, job)
                    except (ResultFileMissing, ResultFileInvalid):
                        self.completion.persist_abandoned(
                            job,
                            worker_id=worker_id,
                            current=current,
                            stale_before=stale_before,
                        )
                        summary["abandoned"] += 1
                    else:
                        self.completion.persist_completion(
                            job,
                            executor_result_from_job_result(persisted),
                            completed_at=persisted.completed_at,
                            state_updated_at=current,
                            condition_sql=(
                                "(heartbeat_at IS NULL OR heartbeat_at < ?)"
                            ),
                            condition_values=(stale_before,),
                            persisted_contract_version=(
                                self.identity.validate_persisted_result(
                                    persisted
                                )
                            ),
                        )
                        summary["recovered"] += 1
                    with contextlib.suppress(
                        OSError,
                        ValueError,
                        BrokerError,
                    ):
                        cleanup_job_protocol_files(
                            self.settings,
                            job.job_id,
                        )
                return summary
            except (sqlite3.Error, ValueError, TypeError) as error:
                if isinstance(error, BrokerDatabaseError):
                    raise
                raise BrokerDatabaseError(
                    "stale job reconciliation failed"
                ) from error
        finally:
            protocol_lock.release()

    def acknowledge_uncertain_job(
        self,
        job_id: str,
        *,
        confirmed_remote_checked: bool,
        now: float | None = None,
    ) -> StableCode:
        if not confirmed_remote_checked:
            raise UncertainJobAcknowledgementRejected(
                "explicit remote-check confirmation is required"
            )
        try:
            canonical_job_id = str(uuid.UUID(job_id))
        except ValueError as error:
            raise UncertainJobAcknowledgementRejected(
                "uncertain Job ID is invalid"
            ) from error
        if canonical_job_id != job_id:
            raise UncertainJobAcknowledgementRejected(
                "uncertain Job ID is not canonical"
            )

        preliminary = self.reconcile_uncertain_results(
            job_id=canonical_job_id
        )
        if preliminary["recovered"] == 1:
            return self.persisted_stable_code(canonical_job_id)

        ensure_private_file(self.settings.protocol_lock_file)
        protocol_lock = FileLock(self.settings.protocol_lock_file)
        protocol_lock.acquire(blocking=True)
        try:
            raced = self.reconcile_uncertain_results_locked(
                job_id=canonical_job_id
            )
            if raced["recovered"] == 1:
                return self.persisted_stable_code(canonical_job_id)
            _, staged_result = result_paths(
                self.settings,
                canonical_job_id,
            )
            if os.path.lexists(staged_result):
                validate_job_protocol_files(
                    self.settings,
                    canonical_job_id,
                )
                raise ActiveOwnerAuthority(
                    "late result publication is in progress"
                )
            try:
                with self.database.connection() as connection:
                    row = connection.execute(
                        "SELECT * FROM jobs WHERE job_id = ?",
                        (canonical_job_id,),
                    ).fetchone()
                if row is None:
                    raise UncertainJobAcknowledgementRejected(
                        "uncertain Job does not exist"
                    )
                if (
                    JobState(str(row["state"])) is not JobState.FAILED
                    or str(row["result_code"]) != _UNKNOWN_RESULT_CODE
                ):
                    raise UncertainJobAcknowledgementRejected(
                        "Job is not awaiting uncertain-result reconciliation"
                    )
                job = self.completion.row_to_job(row)
                diagnostic, persisted = self.late_result(job)
                if (
                    persisted is not None
                    or diagnostic != _LATE_RESULT_MISSING
                ):
                    raise ProtocolStateError(
                        "late result requires safe automatic reconciliation"
                    )
                raw_result = row["result_json"]
                if not isinstance(raw_result, str):
                    raise BrokerDatabaseError(
                        "uncertain Job result is missing"
                    )
                try:
                    uncertain_result = JobResult.from_json(
                        raw_result,
                        disposition=SubmitDisposition.CREATED,
                    )
                    if (
                        uncertain_result.job_id != job.job_id
                        or uncertain_result.game != job.game
                        or uncertain_result.operation != job.operation
                    ):
                        raise ValueError(
                            "uncertain Job result identity was invalid"
                        )
                    contract_version = (
                        self.identity.validate_persisted_result(
                            uncertain_result
                        )
                    )
                except ValueError as error:
                    raise BrokerDatabaseError(
                        "uncertain Job result contract was invalid"
                    ) from error
                validate_job_protocol_files(
                    self.settings,
                    canonical_job_id,
                )
                worker_held = os.path.lexists(
                    self.settings.worker_lock_file
                ) and is_lock_held(self.settings.worker_lock_file)
                profile_held = os.path.lexists(
                    self.settings.profile_lock_file
                ) and is_lock_held(self.settings.profile_lock_file)
                if worker_held or profile_held:
                    raise ActiveOwnerAuthority(
                        "active browser authority blocks uncertain acknowledgement"
                    )

                current = time.time() if now is None else now
                self.completion.acknowledge_uncertain_completion(
                    job,
                    contract_version=contract_version,
                    current=current,
                )
                cleanup_job_protocol_files(
                    self.settings,
                    canonical_job_id,
                )
                return StableCode.PUBLISH_OUTCOME_UNKNOWN_ACKNOWLEDGED
            except sqlite3.Error as error:
                raise BrokerDatabaseError(
                    "uncertain acknowledgement failed"
                ) from error
        finally:
            protocol_lock.release()

    def persisted_stable_code(self, job_id: str) -> StableCode:
        try:
            with self.database.connection() as connection:
                row = connection.execute(
                    "SELECT result_code FROM jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
            if row is None or row["result_code"] is None:
                raise BrokerDatabaseError("recovered Job result is missing")
            return StableCode(str(row["result_code"]))
        except (sqlite3.Error, ValueError) as error:
            if isinstance(error, BrokerDatabaseError):
                raise
            raise BrokerDatabaseError(
                "recovered Job result could not be read"
            ) from error

    def inspect_uncertain_jobs(
        self,
        *,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        ensure_private_file(self.settings.protocol_lock_file)
        protocol_lock = FileLock(self.settings.protocol_lock_file)
        protocol_lock.acquire(blocking=True)
        try:
            try:
                with self.database.connection() as connection:
                    rows = connection.execute(
                        """
                        SELECT *
                        FROM jobs
                        WHERE result_code IN (
                            'PUBLISH_OUTCOME_UNKNOWN',
                            'PUBLISH_OUTCOME_UNKNOWN_ACKNOWLEDGED'
                        )
                          AND state = 'FAILED'
                        ORDER BY created_at DESC, job_id DESC
                        LIMIT ?
                        """,
                        (limit,),
                    ).fetchall()
                inspected: list[dict[str, object]] = []
                for row in rows:
                    job = self.completion.row_to_job(row)
                    stable_code = str(row["result_code"])
                    if stable_code == _ACKNOWLEDGED_RESULT_CODE:
                        reconciliation_status = "ACKNOWLEDGED"
                        diagnostic = _ACKNOWLEDGED_RESULT_CODE
                    else:
                        try:
                            validate_job_protocol_files(
                                self.settings,
                                job.job_id,
                            )
                        except ProtocolStateError:
                            diagnostic = _LATE_RESULT_UNSAFE
                        else:
                            diagnostic, _ = self.late_result(job)
                        reconciliation_status = (
                            "LATE_RESULT_AVAILABLE"
                            if diagnostic == _LATE_RESULT_AVAILABLE
                            else "UNRESOLVED"
                        )
                    inspected.append(
                        {
                            "job_id": str(row["job_id"]),
                            "game": str(row["game"]),
                            "created_at": float(row["created_at"]),
                            "started_at": (
                                float(row["started_at"])
                                if row["started_at"] is not None
                                else None
                            ),
                            "state": str(row["state"]),
                            "stable_code": stable_code,
                            "reconciliation_status": reconciliation_status,
                            "diagnostic_code": diagnostic,
                            "result_file_exists": os.path.lexists(
                                result_paths(
                                    self.settings,
                                    str(row["job_id"]),
                                )[0]
                            ),
                        }
                    )
                return inspected
            except (sqlite3.Error, TypeError, ValueError) as error:
                raise BrokerDatabaseError(
                    "uncertain job inspection failed"
                ) from error
        finally:
            protocol_lock.release()
