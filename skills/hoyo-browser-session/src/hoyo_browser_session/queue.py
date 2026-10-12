"""Compatibility facade for decomposed transactional Queue authorities."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from .config import ACCOUNT_SCOPE, Settings, production_settings
from .db import Database
from .errors import (
    BrokerDatabaseError,
    BrokerError,
    ConfigurationError,
    JobWaitTimeout,
    ProtocolStateError,
)
from .execution_identity import ExecutionIdentityAuthority
from .history_retention import HistoryRetention, RetentionMaintenanceResult
from .locks import FileLock, ensure_private_file
from .maintenance_state_store import MaintenanceStateStore
from .models import (
    AccountState,
    ClaimOutcome,
    ExecutorResult,
    Job,
    JobParameters,
    JobResult,
    JobState,
    OwnerRecord,
    StableCode,
    Submission,
    SubmitDisposition,
)
from .operation import OperationAdapter
from .operational_events import OperationalEventStore, OperationalEventType
from .owner_state_store import OwnerStateStore
from .queue_completion import CompletionAuthority
from .queue_dispatch import DispatchAuthority
from .queue_reconciliation import ReconciliationAuthority
from .queue_submission import SubmissionAuthority
from .worker_protocol import (
    classify_protocol_inventory,
    cleanup_job_protocol_files,
)

_TERMINAL_STATES = frozenset(
    {
        JobState.SUCCEEDED,
        JobState.FAILED,
        JobState.BLOCKED,
        JobState.ABANDONED,
    }
)
_UNKNOWN_RESULT_CODE = StableCode.PUBLISH_OUTCOME_UNKNOWN.value


class JobQueue:
    """Stable public facade composed from single-purpose Queue authorities."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.database = Database(settings)
        self.database.initialize()
        self._events = OperationalEventStore(settings, self.database)
        self._retention = HistoryRetention(settings, self.database)
        self._maintenance_state = MaintenanceStateStore(
            self.database,
            self._events,
        )
        self._identity = ExecutionIdentityAuthority(settings)
        self._owner_state = OwnerStateStore(
            settings,
            self.database,
            self._events,
        )
        self._completion = CompletionAuthority(
            settings,
            self.database,
            self._identity,
            self._owner_state,
            self._events,
            self._cleanup_after_terminal,
        )
        self._submission = SubmissionAuthority(
            settings,
            self.database,
            self._identity,
            self._events,
        )
        self._dispatch = DispatchAuthority(
            settings,
            self.database,
            self._identity,
            self._completion,
            self._events,
        )
        self._reconciliation = ReconciliationAuthority(
            settings,
            self.database,
            self._identity,
            self._completion,
        )

    def _job_exists(self, job_id: str) -> bool:
        return self._submission._job_exists(job_id)

    def _validate_request(
        self,
        game: str,
        operation: str,
        repo_root: Path,
        *,
        runtime_digest: str | None = None,
    ) -> tuple[Path, OperationAdapter | None, str]:
        return self._identity.validate_request(
            game,
            operation,
            repo_root,
            runtime_digest=runtime_digest,
        )

    def _active_runtime_digest(self) -> str:
        return self._identity.active_runtime_digest()

    def _job_execution_identity_matches(
        self,
        job: Job,
        *,
        runtime_digest: str,
    ) -> bool:
        return self._identity.job_matches(
            job,
            runtime_digest=runtime_digest,
        )

    def _job_contract_version(self, job: Job) -> int:
        return self._identity.job_contract_version(job)

    def _terminalize_execution_identity_mismatch(
        self,
        connection: sqlite3.Connection,
        job: Job,
        *,
        current: float,
    ) -> None:
        self._completion.terminalize_execution_identity_mismatch(
            connection,
            job,
            current=current,
        )

    def submit(
        self,
        *,
        game: str,
        operation: str,
        repo_root: Path,
        parameters: JobParameters | None = None,
        force: bool = False,
        now: float | None = None,
    ) -> Submission:
        return self._submission.submit(
            game=game,
            operation=operation,
            repo_root=repo_root,
            parameters=parameters,
            force=force,
            now=now,
        )

    def wait(
        self,
        submission: Submission,
        *,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 0.05,
    ) -> JobResult:
        if submission.reused_result is not None:
            return submission.reused_result
        started = time.monotonic()
        waiting_marker_printed = False
        while True:
            result, progress_code = self._terminal_result(submission)
            if progress_code == "WAITING_FOR_USER_LOGIN" and not waiting_marker_printed:
                print("WAITING_FOR_USER_LOGIN=YES", flush=True)
                waiting_marker_printed = True
            if result is not None:
                return result
            if timeout_seconds is not None and time.monotonic() - started >= timeout_seconds:
                raise JobWaitTimeout("job wait timed out")
            time.sleep(poll_interval_seconds)

    def _terminal_result(
        self,
        submission: Submission,
    ) -> tuple[JobResult | None, str | None]:
        try:
            with self.database.connection() as connection:
                row = connection.execute(
                    """
                    SELECT state, result_json, progress_code
                    FROM jobs
                    WHERE job_id = ?
                    """,
                    (submission.job_id,),
                ).fetchone()
            if row is None:
                raise BrokerDatabaseError("submitted job no longer exists")
            state = JobState(str(row["state"]))
            progress = row["progress_code"]
            progress_code = str(progress) if progress is not None else None
            if state not in _TERMINAL_STATES:
                return None, progress_code
            value = row["result_json"]
            if not isinstance(value, str):
                raise BrokerDatabaseError("terminal job result is missing")
            persisted = JobResult.from_json(
                value,
                disposition=submission.disposition,
            )
            self._identity.validate_persisted_result(persisted)
            return persisted, progress_code
        except (sqlite3.Error, ValueError) as error:
            if isinstance(error, BrokerDatabaseError):
                raise
            raise BrokerDatabaseError("job result could not be read") from error

    def claim_next(
        self,
        worker_id: str,
        *,
        now: float | None = None,
        runtime_digest: str | None = None,
    ) -> ClaimOutcome:
        return self._dispatch.claim_next(
            worker_id,
            now=now,
            runtime_digest=runtime_digest,
        )

    def finish(
        self,
        job: Job,
        worker_id: str,
        executor_result: ExecutorResult,
        *,
        now: float | None = None,
    ) -> str:
        return self._completion.finish(
            job,
            worker_id,
            executor_result,
            now=now,
        )

    def _persist_completion(
        self,
        job: Job,
        executor_result: ExecutorResult,
        *,
        completed_at: float,
        state_updated_at: float,
        condition_sql: str,
        condition_values: tuple[object, ...],
        persisted_contract_version: int | None = None,
    ) -> None:
        self._completion.persist_completion(
            job,
            executor_result,
            completed_at=completed_at,
            state_updated_at=state_updated_at,
            condition_sql=condition_sql,
            condition_values=condition_values,
            persisted_contract_version=persisted_contract_version,
        )

    @staticmethod
    def _account_transition(
        code: StableCode,
    ) -> tuple[AccountState, str | None]:
        return CompletionAuthority.account_transition(code)

    def mark_login_waiting(self, job_id: str) -> None:
        try:
            import uuid

            uuid.UUID(job_id)
            with self.database.immediate() as connection:
                updated = connection.execute(
                    """
                    UPDATE jobs
                    SET progress_code = 'WAITING_FOR_USER_LOGIN'
                    WHERE job_id = ? AND state = 'RUNNING'
                    """,
                    (job_id,),
                )
                if updated.rowcount != 1:
                    raise BrokerDatabaseError("job login progress lost its authority")
        except (sqlite3.Error, ValueError) as error:
            if isinstance(error, BrokerDatabaseError):
                raise
            raise BrokerDatabaseError("job login progress could not be recorded") from error

    def sweep_protocol_files(self) -> int:
        """Delete only proven orphan or terminal files under one lock."""

        ensure_private_file(self.settings.protocol_lock_file)
        protocol_lock = FileLock(self.settings.protocol_lock_file)
        protocol_lock.acquire(blocking=True)
        removed_jobs: set[str] = set()
        try:
            try:
                inventory = classify_protocol_inventory(
                    self.settings,
                    strict=True,
                )
                with self.database.connection() as connection:
                    rows = connection.execute(
                        "SELECT job_id, state, result_code FROM jobs"
                    ).fetchall()
                states = {
                    str(row["job_id"]): (
                        JobState(str(row["state"])),
                        (str(row["result_code"]) if row["result_code"] is not None else None),
                    )
                    for row in rows
                }
                for entry in inventory.entries:
                    persisted = states.get(entry.job_id)
                    if persisted is None:
                        cleanup_job_protocol_files(
                            self.settings,
                            entry.job_id,
                        )
                        removed_jobs.add(entry.job_id)
                        continue
                    state, result_code = persisted
                    if state is JobState.FAILED and result_code == _UNKNOWN_RESULT_CODE:
                        continue
                    if state in _TERMINAL_STATES:
                        cleanup_job_protocol_files(
                            self.settings,
                            entry.job_id,
                        )
                        removed_jobs.add(entry.job_id)
                return len(removed_jobs)
            except sqlite3.Error as error:
                raise BrokerDatabaseError("protocol sweep database read failed") from error
        finally:
            protocol_lock.release()

    def _cleanup_after_terminal(self, job_id: str) -> str:
        protocol_lock: FileLock | None = None
        try:
            ensure_private_file(self.settings.protocol_lock_file)
            protocol_lock = FileLock(self.settings.protocol_lock_file)
            protocol_lock.acquire(blocking=True)
            with self.database.connection() as connection:
                row = connection.execute(
                    """
                    SELECT state, result_code
                    FROM jobs
                    WHERE job_id = ?
                    """,
                    (job_id,),
                ).fetchone()
            if row is None or JobState(str(row["state"])) not in _TERMINAL_STATES:
                raise ProtocolStateError("protocol cleanup requires a terminal Job")
            if (
                JobState(str(row["state"])) is JobState.FAILED
                and str(row["result_code"]) == _UNKNOWN_RESULT_CODE
            ):
                return "PROTOCOL_PRESERVED_UNCERTAIN"
            cleanup_job_protocol_files(self.settings, job_id)
            return "PROTOCOL_CLEAN"
        except (OSError, ValueError, BrokerError):
            return "PROTOCOL_CLEANUP_PENDING"
        finally:
            if protocol_lock is not None:
                protocol_lock.release()

    def clear_authentication_block(self) -> None:
        self._completion.clear_authentication_block()

    def block_login_terminal(self, code: StableCode) -> None:
        self._completion.block_login_terminal(code)

    def _late_result(self, job: Job) -> tuple[str, JobResult | None]:
        return self._reconciliation.late_result(job)

    def reconcile_uncertain_results(
        self,
        *,
        job_id: str | None = None,
        now: float | None = None,
    ) -> dict[str, int]:
        return self._reconciliation.reconcile_uncertain_results(
            job_id=job_id,
            now=now,
        )

    def _reconcile_uncertain_results_locked(
        self,
        *,
        job_id: str | None = None,
        now: float | None = None,
    ) -> dict[str, int]:
        return self._reconciliation.reconcile_uncertain_results_locked(
            job_id=job_id,
            now=now,
        )

    def _persist_uncertain_completion(
        self,
        job: Job,
        persisted: JobResult,
        *,
        state_updated_at: float,
    ) -> None:
        self._completion.persist_uncertain_completion(
            job,
            persisted,
            state_updated_at=state_updated_at,
        )

    @staticmethod
    def _profile_state_is_broken(
        connection: sqlite3.Connection,
    ) -> bool:
        return CompletionAuthority.profile_state_is_broken(connection)

    def reconcile_stale_running(
        self,
        worker_id: str,
        *,
        now: float | None = None,
    ) -> dict[str, int]:
        return self._reconciliation.reconcile_stale_running(
            worker_id,
            now=now,
        )

    def _persist_abandoned(
        self,
        job: Job,
        *,
        worker_id: str,
        current: float,
        stale_before: float,
    ) -> None:
        self._completion.persist_abandoned(
            job,
            worker_id=worker_id,
            current=current,
            stale_before=stale_before,
        )

    def heartbeat(
        self,
        worker_id: str,
        job_id: str | None = None,
        *,
        runtime_digest: str | None = None,
    ) -> None:
        self._owner_state.heartbeat(
            worker_id,
            job_id,
            runtime_digest=runtime_digest,
        )

    def begin_owner(self, worker_id: str, *, runtime_digest: str) -> None:
        self._owner_state.begin_owner(
            worker_id,
            runtime_digest=runtime_digest,
        )

    def mark_owner_running(
        self,
        worker_id: str,
        *,
        context_generation: str,
    ) -> None:
        self._owner_state.mark_owner_running(
            worker_id,
            context_generation=context_generation,
        )

    def mark_owner_broken(self, worker_id: str) -> None:
        self._owner_state.mark_owner_broken(worker_id)

    def break_owner_and_terminalize_queued(
        self,
        worker_id: str,
        *,
        now: float | None = None,
        persistent_profile_broken: bool = False,
    ) -> int:
        return self._completion.break_owner_and_terminalize_queued(
            worker_id,
            now=now,
            persistent_profile_broken=persistent_profile_broken,
        )

    def clear_worker(self, worker_id: str) -> None:
        self._owner_state.clear_worker(worker_id)

    def worker_heartbeat(self) -> tuple[str | None, float | None]:
        return self._owner_state.worker_heartbeat()

    def owner_record(self) -> OwnerRecord:
        return self._owner_state.owner_record()

    def queue_counts(self) -> tuple[int, int]:
        return self._owner_state.queue_counts()

    def commit_owner_stop_if_idle(self, worker_id: str) -> bool:
        return self._owner_state.commit_owner_stop_if_idle(worker_id)

    def progress_code(self, job_id: str) -> str | None:
        try:
            with self.database.connection() as connection:
                row = connection.execute(
                    "SELECT progress_code FROM jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
            if row is None:
                raise BrokerDatabaseError("Job progress row is missing")
            value = row["progress_code"]
            return str(value) if value is not None else None
        except sqlite3.Error as error:
            raise BrokerDatabaseError("Job progress could not be read") from error

    def require_login_after_owner_stop(self) -> None:
        self._completion.require_login_after_owner_stop()

    def queued_count(self) -> int:
        try:
            with self.database.connection() as connection:
                row = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM jobs
                    WHERE state IN ('QUEUED', 'RUNNING')
                    """
                ).fetchone()
            if row is None:
                raise BrokerDatabaseError("queued job count is missing")
            return int(row[0])
        except (sqlite3.Error, TypeError, ValueError) as error:
            if isinstance(error, BrokerDatabaseError):
                raise
            raise BrokerDatabaseError("queued job count could not be read") from error

    def clear_test_block(self) -> None:
        if not self.settings.test_mode:
            raise ConfigurationError("risk state can only be cleared by an explicit test command")
        current = time.time()
        try:
            with self.database.immediate() as connection:
                updated = connection.execute(
                    """
                    UPDATE account_state
                    SET state = 'HEALTHY', next_job_at = 0,
                        blocked_reason = NULL, updated_at = ?
                    WHERE account_scope = ?
                    """,
                    (current, ACCOUNT_SCOPE),
                )
                if updated.rowcount != 1:
                    raise BrokerDatabaseError("test block clear lost its authority")
        except sqlite3.Error as error:
            raise BrokerDatabaseError("test block clear failed") from error

    def acknowledge_uncertain_job(
        self,
        job_id: str,
        *,
        confirmed_remote_checked: bool,
        now: float | None = None,
    ) -> StableCode:
        return self._reconciliation.acknowledge_uncertain_job(
            job_id,
            confirmed_remote_checked=confirmed_remote_checked,
            now=now,
        )

    def run_retention_maintenance(
        self,
        *,
        now: float | None = None,
    ) -> RetentionMaintenanceResult:
        return self._retention.maintain(now=now)

    def observe_retention_maintenance(
        self,
        result: RetentionMaintenanceResult,
    ) -> bool:
        try:
            return self._maintenance_state.observe(result)
        except Exception:
            # An unavailable broker DB is already a first-class diagnostic.
            # Maintenance observability cannot reclassify or block core work.
            return False

    def record_runtime_mismatch(self) -> None:
        self._events.record(
            OperationalEventType.RUNTIME_MISMATCH,
            stable_code=StableCode.OWNER_RUNTIME_RESTART_REQUIRED,
        )

    def _persisted_stable_code(self, job_id: str) -> StableCode:
        return self._reconciliation.persisted_stable_code(job_id)

    def inspect_jobs(self, *, limit: int = 50) -> list[dict[str, object]]:
        try:
            with self.database.connection() as connection:
                rows = connection.execute(
                    """
                    SELECT job_id, game, operation, state, created_at,
                           started_at, finished_at, result_code
                    FROM jobs
                    ORDER BY created_at DESC, job_id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            return [
                {
                    "job_id": str(row["job_id"]),
                    "game": str(row["game"]),
                    "operation": str(row["operation"]),
                    "state": str(row["state"]),
                    "created_at": float(row["created_at"]),
                    "started_at": (
                        float(row["started_at"]) if row["started_at"] is not None else None
                    ),
                    "finished_at": (
                        float(row["finished_at"]) if row["finished_at"] is not None else None
                    ),
                    "result_code": (
                        str(row["result_code"]) if row["result_code"] is not None else None
                    ),
                }
                for row in rows
            ]
        except (sqlite3.Error, TypeError, ValueError) as error:
            raise BrokerDatabaseError("job inspection failed") from error

    def inspect_uncertain_jobs(
        self,
        *,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        return self._reconciliation.inspect_uncertain_jobs(limit=limit)

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        return CompletionAuthority.row_to_job(row)

    @staticmethod
    def _blocked_code(state: AccountState, persisted: Any) -> StableCode:
        return DispatchAuthority.blocked_code(state, persisted)


def submit_and_wait(
    *,
    game: str,
    operation: str,
    repo_root: Path,
    parameters: JobParameters,
    force: bool = False,
    timeout_seconds: float | None = None,
) -> JobResult:
    """Production API using only fixed Runtime, database, and Profile paths."""

    from .owner import assert_owner_submit_admissible, ensure_owner
    from .runtime import active_runtime_python, read_active_runtime

    settings = production_settings()
    runtime_python = active_runtime_python(settings)
    active_digest = str(read_active_runtime(settings)["runtime_digest"])
    owner_ready = assert_owner_submit_admissible(
        settings,
        active_runtime_digest=active_digest,
    )
    queue = JobQueue(settings)
    submission = queue.submit(
        game=game,
        operation=operation,
        repo_root=repo_root,
        parameters=parameters,
        force=force,
    )
    if submission.disposition is not SubmitDisposition.REUSED and not owner_ready:
        ensure_owner(
            settings,
            runtime_python=runtime_python,
            repo_root=repo_root,
        )
    return queue.wait(
        submission,
        timeout_seconds=timeout_seconds,
    )


def submit_and_wait_with_settings(
    settings: Settings,
    *,
    game: str,
    operation: str,
    repo_root: Path,
    runtime_python: Path,
    parameters: JobParameters | None = None,
    force: bool = False,
    timeout_seconds: float | None = None,
) -> JobResult:
    """Internal focused-test entrypoint; not a production API."""

    from .owner import assert_owner_submit_admissible, ensure_owner

    queue = JobQueue(settings)
    owner_ready = assert_owner_submit_admissible(
        settings,
        active_runtime_digest="0" * 64,
    )
    submission = queue.submit(
        game=game,
        operation=operation,
        repo_root=repo_root,
        parameters=parameters,
        force=force,
    )
    if submission.disposition is not SubmitDisposition.REUSED and not owner_ready:
        ensure_owner(
            settings,
            runtime_python=runtime_python,
            repo_root=repo_root,
        )
    return queue.wait(
        submission,
        timeout_seconds=timeout_seconds,
    )


def mark_login_waiting(job_id: str) -> None:
    """Record one safe progress marker for the submitting client."""

    JobQueue(production_settings()).mark_login_waiting(job_id)


def mark_login_verified() -> None:
    """Recover AUTH_REQUIRED only after a successful Binding check."""

    JobQueue(production_settings()).clear_authentication_block()
