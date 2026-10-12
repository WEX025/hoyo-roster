"""Authoritative Job terminal and account-state transitions."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable

from .config import (
    ACCOUNT_SCOPE,
    PROFILE_ID,
    SYNTHETIC_OPERATION_CONTRACT_VERSION,
    SYNTHETIC_OPERATIONS,
    Settings,
)
from .db import Database
from .errors import BrokerDatabaseError
from .execution_identity import ExecutionIdentityAuthority
from .models import (
    AccountState,
    ExecutorResult,
    Job,
    JobResult,
    JobState,
    StableCode,
    result_json,
)
from .operation_registry import operation_adapter
from .operational_events import OperationalEventStore, OperationalEventType
from .owner_state_store import OwnerStateStore
from .worker_protocol import executor_result_from_job_result

_UNKNOWN_RESULT_CODE = StableCode.PUBLISH_OUTCOME_UNKNOWN.value
_ACKNOWLEDGED_RESULT_CODE = StableCode.PUBLISH_OUTCOME_UNKNOWN_ACKNOWLEDGED.value
_BLOCKING_ACCOUNT_STATES = frozenset(
    {
        AccountState.AUTH_REQUIRED,
        AccountState.RATE_LIMITED,
        AccountState.RISK_CONTROL,
        AccountState.PROFILE_BROKEN,
        AccountState.RECONCILIATION_REQUIRED,
    }
)


class CompletionAuthority:
    """Own every atomic Job terminal and account-state transaction."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        identity: ExecutionIdentityAuthority,
        owner_state: OwnerStateStore,
        events: OperationalEventStore,
        cleanup_after_terminal: Callable[[str], str],
    ) -> None:
        self.settings = settings
        self.database = database
        self.identity = identity
        self.owner_state = owner_state
        self.events = events
        self.cleanup_after_terminal = cleanup_after_terminal

    def terminalize_execution_identity_mismatch(
        self,
        connection: sqlite3.Connection,
        job: Job,
        *,
        current: float,
    ) -> None:
        result = ExecutorResult(stable_code=StableCode.OWNER_RUNTIME_RESTART_REQUIRED)
        contract_version = self.identity.job_contract_version(job)
        adapter = operation_adapter(job.game, job.operation)
        if adapter is not None:
            adapter.validate_persisted_result(result, contract_version)
        elif not (
            self.settings.test_mode
            and job.operation in SYNTHETIC_OPERATIONS
            and contract_version == SYNTHETIC_OPERATION_CONTRACT_VERSION
        ):
            raise BrokerDatabaseError("queued Job operation contract was invalid")
        payload = result_json(
            job=job,
            status=JobState.FAILED,
            result=result,
            completed_at=current,
            operation_contract_version=contract_version,
        )
        updated = connection.execute(
            """
            UPDATE jobs
            SET state = 'FAILED', finished_at = ?, heartbeat_at = ?,
                result_code = 'OWNER_RUNTIME_RESTART_REQUIRED',
                result_json = ?, progress_code = NULL
            WHERE job_id = ? AND state = 'QUEUED'
            """,
            (current, current, payload, job.job_id),
        )
        if updated.rowcount != 1:
            raise BrokerDatabaseError("queued Job generation rejection lost its authority")
        self.events.record_terminal_in_transaction(
            connection,
            job,
            state=JobState.FAILED,
            stable_code=result.stable_code,
            occurred_at=current,
        )
        account = connection.execute(
            "SELECT state FROM account_state WHERE account_scope = ?",
            (ACCOUNT_SCOPE,),
        ).fetchone()
        if account is None:
            raise BrokerDatabaseError("primary account state is missing")
        if AccountState(str(account["state"])) not in _BLOCKING_ACCOUNT_STATES:
            account_updated = connection.execute(
                """
                UPDATE account_state
                SET state = 'COOLDOWN', next_job_at = ?, blocked_reason = NULL,
                    updated_at = ?
                WHERE account_scope = ?
                """,
                (
                    current + self.settings.post_job_cooldown_seconds,
                    current,
                    ACCOUNT_SCOPE,
                ),
            )
            if account_updated.rowcount != 1:
                raise BrokerDatabaseError("queued Job generation account transition failed")

    def block_queued_job(
        self,
        connection: sqlite3.Connection,
        job: Job,
        code: StableCode,
        *,
        current: float,
    ) -> None:
        blocked_result = ExecutorResult(stable_code=code)
        payload = result_json(
            job=job,
            status=JobState.BLOCKED,
            result=blocked_result,
            completed_at=current,
            operation_contract_version=self.identity.validate_live_result(
                job,
                blocked_result,
            ),
        )
        updated = connection.execute(
            """
            UPDATE jobs
            SET state = 'BLOCKED', finished_at = ?,
                result_code = ?, result_json = ?
            WHERE job_id = ? AND state = 'QUEUED'
            """,
            (current, code.value, payload, job.job_id),
        )
        if updated.rowcount != 1:
            raise BrokerDatabaseError("blocked job transition lost its authority")
        self.events.record_terminal_in_transaction(
            connection,
            job,
            state=JobState.BLOCKED,
            stable_code=code,
            occurred_at=current,
        )

    def finish(
        self,
        job: Job,
        worker_id: str,
        executor_result: ExecutorResult,
        *,
        now: float | None = None,
    ) -> str:
        """Commit the terminal row before attempting protocol file cleanup."""

        current = time.time() if now is None else now
        self.persist_completion(
            job,
            executor_result,
            completed_at=current,
            state_updated_at=current,
            condition_sql="worker_id = ?",
            condition_values=(worker_id,),
        )
        return self.cleanup_after_terminal(job.job_id)

    def persist_completion(
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
        adapter = operation_adapter(job.game, job.operation)
        if persisted_contract_version is None:
            contract_version = self.identity.validate_live_result(
                job,
                executor_result,
            )
        elif adapter is not None:
            adapter.validate_persisted_result(
                executor_result,
                persisted_contract_version,
            )
            contract_version = persisted_contract_version
        elif (
            self.settings.test_mode
            and job.operation in SYNTHETIC_OPERATIONS
            and persisted_contract_version == SYNTHETIC_OPERATION_CONTRACT_VERSION
        ):
            contract_version = persisted_contract_version
        else:
            raise ValueError("persisted job operation contract was invalid")
        terminal = (
            JobState.SUCCEEDED
            if executor_result.stable_code is StableCode.PASS
            else JobState.FAILED
        )
        payload = result_json(
            job=job,
            status=terminal,
            result=executor_result,
            completed_at=completed_at,
            operation_contract_version=contract_version,
        )
        account_state, blocked_reason = self.account_transition(executor_result.stable_code)
        next_job_at = (
            0.0
            if account_state is AccountState.HEALTHY
            else state_updated_at + self.settings.post_job_cooldown_seconds
        )
        try:
            with self.database.immediate() as connection:
                updated = connection.execute(
                    f"""
                    UPDATE jobs
                    SET state = ?, finished_at = ?, heartbeat_at = ?,
                        result_code = ?, result_json = ?
                    WHERE job_id = ? AND state = 'RUNNING' AND {condition_sql}
                    """,
                    (
                        terminal.value,
                        completed_at,
                        state_updated_at,
                        executor_result.stable_code.value,
                        payload,
                        job.job_id,
                        *condition_values,
                    ),
                )
                if updated.rowcount != 1:
                    raise BrokerDatabaseError("job completion lost its authority")
                account_updated = connection.execute(
                    """
                    UPDATE account_state
                    SET state = ?, next_job_at = ?, blocked_reason = ?,
                        last_success_game = CASE
                            WHEN ? = 'SUCCEEDED' THEN ? ELSE last_success_game
                        END,
                        last_success_at = CASE
                            WHEN ? = 'SUCCEEDED' THEN ? ELSE last_success_at
                        END,
                        updated_at = ?
                    WHERE account_scope = ?
                    """,
                    (
                        account_state.value,
                        next_job_at,
                        blocked_reason,
                        terminal.value,
                        job.game,
                        terminal.value,
                        completed_at,
                        state_updated_at,
                        ACCOUNT_SCOPE,
                    ),
                )
                if account_updated.rowcount != 1:
                    raise BrokerDatabaseError("account completion transition failed")
                self.events.record_terminal_in_transaction(
                    connection,
                    job,
                    state=terminal,
                    stable_code=executor_result.stable_code,
                    occurred_at=state_updated_at,
                )
        except sqlite3.Error as error:
            raise BrokerDatabaseError("job completion failed") from error

    @staticmethod
    def account_transition(
        code: StableCode,
    ) -> tuple[AccountState, str | None]:
        if code is StableCode.RATE_LIMITED:
            return AccountState.RATE_LIMITED, StableCode.RATE_LIMITED.value
        if code is StableCode.RISK_CONTROL:
            return AccountState.RISK_CONTROL, StableCode.RISK_CONTROL.value
        if code in {
            StableCode.AUTHENTICATION_REQUIRED,
            StableCode.LOGIN_TIMEOUT,
            StableCode.LOGIN_NETWORK_ERROR,
            StableCode.LOGIN_BINDING_FAILED,
            StableCode.WRONG_ACCOUNT,
        }:
            return (
                AccountState.AUTH_REQUIRED,
                StableCode.AUTHENTICATION_REQUIRED.value,
            )
        if code is StableCode.PROFILE_BROKEN:
            return AccountState.PROFILE_BROKEN, StableCode.PROFILE_BROKEN.value
        if code is StableCode.PUBLISH_OUTCOME_UNKNOWN:
            return (
                AccountState.RECONCILIATION_REQUIRED,
                StableCode.PUBLISH_OUTCOME_UNKNOWN.value,
            )
        if code is StableCode.UID_REQUIRED:
            return AccountState.HEALTHY, None
        return AccountState.COOLDOWN, None

    def clear_authentication_block(self) -> None:
        """Clear only AUTH_REQUIRED after an explicit successful Binding check."""

        try:
            with self.database.immediate() as connection:
                connection.execute(
                    """
                    UPDATE account_state
                    SET state = 'HEALTHY', next_job_at = 0,
                        blocked_reason = NULL, updated_at = ?
                    WHERE account_scope = ? AND state = 'AUTH_REQUIRED'
                    """,
                    (time.time(), ACCOUNT_SCOPE),
                )
        except sqlite3.Error as error:
            raise BrokerDatabaseError("authentication block clear failed") from error

    def block_login_terminal(self, code: StableCode) -> None:
        """Persist only terminal account-wide login probe protections."""

        if code not in {StableCode.RATE_LIMITED, StableCode.RISK_CONTROL}:
            raise ValueError("login terminal block code was invalid")
        account_state, blocked_reason = self.account_transition(code)
        try:
            with self.database.immediate() as connection:
                updated = connection.execute(
                    """
                    UPDATE account_state
                    SET state = ?, next_job_at = 0, blocked_reason = ?, updated_at = ?
                    WHERE account_scope = ?
                    """,
                    (
                        account_state.value,
                        blocked_reason,
                        time.time(),
                        ACCOUNT_SCOPE,
                    ),
                )
                if updated.rowcount != 1:
                    raise BrokerDatabaseError("login terminal account transition failed")
        except sqlite3.Error as error:
            raise BrokerDatabaseError("login terminal account transition failed") from error

    def persist_uncertain_completion(
        self,
        job: Job,
        persisted: JobResult,
        *,
        state_updated_at: float,
    ) -> None:
        executor_result = executor_result_from_job_result(persisted)
        contract_version = self.identity.validate_persisted_result(persisted)
        terminal = (
            JobState.SUCCEEDED
            if executor_result.stable_code is StableCode.PASS
            else JobState.FAILED
        )
        payload = result_json(
            job=job,
            status=terminal,
            result=executor_result,
            completed_at=persisted.completed_at,
            operation_contract_version=contract_version,
        )
        account_state, blocked_reason = self.account_transition(executor_result.stable_code)
        next_job_at = (
            0.0
            if account_state is AccountState.HEALTHY
            else state_updated_at + self.settings.post_job_cooldown_seconds
        )
        try:
            with self.database.immediate() as connection:
                account = connection.execute(
                    """
                    SELECT state, next_job_at, blocked_reason
                    FROM account_state
                    WHERE account_scope = ?
                    """,
                    (ACCOUNT_SCOPE,),
                ).fetchone()
                if account is None:
                    raise BrokerDatabaseError("primary account state is missing")
                current_account_state = AccountState(str(account["state"]))
                current_blocked_reason = (
                    str(account["blocked_reason"])
                    if account["blocked_reason"] is not None
                    else None
                )
                profile_broken = (
                    current_account_state is AccountState.PROFILE_BROKEN
                    or self.profile_state_is_broken(connection)
                )
                if (
                    current_account_state is AccountState.RECONCILIATION_REQUIRED
                    and current_blocked_reason != _UNKNOWN_RESULT_CODE
                ):
                    raise BrokerDatabaseError("uncertain account state is inconsistent")
                final_blocked_reason: str | None
                if profile_broken:
                    final_account_state = AccountState.PROFILE_BROKEN
                    final_blocked_reason = StableCode.PROFILE_BROKEN.value
                    final_next_job_at = 0.0
                elif current_account_state in {
                    AccountState.AUTH_REQUIRED,
                    AccountState.RATE_LIMITED,
                    AccountState.RISK_CONTROL,
                }:
                    final_account_state = current_account_state
                    final_blocked_reason = current_blocked_reason
                    final_next_job_at = float(account["next_job_at"])
                else:
                    final_account_state = account_state
                    final_blocked_reason = blocked_reason
                    final_next_job_at = next_job_at
                updated = connection.execute(
                    """
                    UPDATE jobs
                    SET state = ?, finished_at = ?, heartbeat_at = ?,
                        result_code = ?, result_json = ?, progress_code = NULL
                    WHERE job_id = ? AND state = 'FAILED'
                      AND result_code = 'PUBLISH_OUTCOME_UNKNOWN'
                    """,
                    (
                        terminal.value,
                        persisted.completed_at,
                        state_updated_at,
                        executor_result.stable_code.value,
                        payload,
                        job.job_id,
                    ),
                )
                if updated.rowcount != 1:
                    raise BrokerDatabaseError("uncertain Job recovery lost its authority")
                account_updated = connection.execute(
                    """
                    UPDATE account_state
                    SET state = ?, next_job_at = ?, blocked_reason = ?,
                        last_success_game = CASE
                            WHEN ? = 'SUCCEEDED' THEN ? ELSE last_success_game
                        END,
                        last_success_at = CASE
                            WHEN ? = 'SUCCEEDED' THEN ? ELSE last_success_at
                        END,
                        updated_at = ?
                    WHERE account_scope = ?
                    """,
                    (
                        final_account_state.value,
                        final_next_job_at,
                        final_blocked_reason,
                        terminal.value,
                        job.game,
                        terminal.value,
                        persisted.completed_at,
                        state_updated_at,
                        ACCOUNT_SCOPE,
                    ),
                )
                if account_updated.rowcount != 1:
                    raise BrokerDatabaseError("uncertain account recovery lost its authority")
                self.events.record_job_event_in_transaction(
                    connection,
                    OperationalEventType.PUBLISH_RECONCILED,
                    job,
                    occurred_at=state_updated_at,
                    stable_code=executor_result.stable_code,
                )
        except sqlite3.Error as error:
            raise BrokerDatabaseError("uncertain completion could not be persisted") from error

    @staticmethod
    def profile_state_is_broken(connection: sqlite3.Connection) -> bool:
        row = connection.execute(
            """
            SELECT state
            FROM profile_state
            WHERE profile_id = ?
            """,
            (PROFILE_ID,),
        ).fetchone()
        return row is not None and str(row["state"]) == "BROKEN"

    def persist_abandoned(
        self,
        job: Job,
        *,
        worker_id: str,
        current: float,
        stale_before: float,
    ) -> None:
        result = ExecutorResult(stable_code=StableCode.JOB_ABANDONED)
        payload = result_json(
            job=job,
            status=JobState.ABANDONED,
            result=result,
            completed_at=current,
            operation_contract_version=self.identity.validate_live_result(
                job,
                result,
            ),
        )
        try:
            with self.database.immediate() as connection:
                updated = connection.execute(
                    """
                    UPDATE jobs
                    SET state = 'ABANDONED', finished_at = ?, worker_id = ?,
                        heartbeat_at = ?, result_code = 'JOB_ABANDONED', result_json = ?
                    WHERE job_id = ? AND state = 'RUNNING'
                      AND (heartbeat_at IS NULL OR heartbeat_at < ?)
                    """,
                    (
                        current,
                        worker_id,
                        current,
                        payload,
                        job.job_id,
                        stale_before,
                    ),
                )
                if updated.rowcount != 1:
                    raise BrokerDatabaseError("stale job abandonment lost its authority")
                account = connection.execute(
                    """
                    SELECT state, next_job_at
                    FROM account_state
                    WHERE account_scope = ?
                    """,
                    (ACCOUNT_SCOPE,),
                ).fetchone()
                if account is None:
                    raise BrokerDatabaseError("primary account state is missing")
                if AccountState(str(account["state"])) not in {
                    AccountState.RATE_LIMITED,
                    AccountState.RISK_CONTROL,
                    AccountState.RECONCILIATION_REQUIRED,
                }:
                    connection.execute(
                        """
                        UPDATE account_state
                        SET state = 'AUTH_REQUIRED', next_job_at = 0,
                            blocked_reason = 'AUTHENTICATION_REQUIRED',
                            updated_at = ?
                        WHERE account_scope = ?
                        """,
                        (current, ACCOUNT_SCOPE),
                    )
                self.events.record_terminal_in_transaction(
                    connection,
                    job,
                    state=JobState.ABANDONED,
                    stable_code=result.stable_code,
                    occurred_at=current,
                )
        except sqlite3.Error as error:
            raise BrokerDatabaseError("stale job abandonment failed") from error

    def break_owner_and_terminalize_queued(
        self,
        worker_id: str,
        *,
        now: float | None = None,
        persistent_profile_broken: bool = False,
    ) -> int:
        """Discard one Owner generation and fail every Job it did not run."""

        current = time.time() if now is None else now
        terminalized_job_ids: list[str] = []
        try:
            with self.database.immediate() as connection:
                account = connection.execute(
                    """
                    SELECT state
                    FROM account_state
                    WHERE account_scope = ?
                    """,
                    (ACCOUNT_SCOPE,),
                ).fetchone()
                if account is None:
                    raise BrokerDatabaseError("primary account state is missing")
                profile_broken = (
                    persistent_profile_broken
                    or AccountState(str(account["state"])) is AccountState.PROFILE_BROKEN
                )
                result = ExecutorResult(
                    stable_code=(
                        StableCode.PROFILE_BROKEN
                        if profile_broken
                        else StableCode.OWNER_RUNTIME_RESTART_REQUIRED
                    )
                )
                terminal_state = JobState.BLOCKED if profile_broken else JobState.FAILED
                self.owner_state.mark_broken_in_transaction(
                    connection,
                    worker_id,
                    current=current,
                )
                rows = connection.execute(
                    """
                    SELECT *
                    FROM jobs
                    WHERE state = 'QUEUED'
                    ORDER BY created_at, job_id
                    """
                ).fetchall()
                for row in rows:
                    job = self.row_to_job(row)
                    payload = result_json(
                        job=job,
                        status=terminal_state,
                        result=result,
                        completed_at=current,
                        operation_contract_version=(
                            self.identity.validate_live_result(job, result)
                        ),
                    )
                    updated = connection.execute(
                        """
                        UPDATE jobs
                        SET state = ?, finished_at = ?,
                            heartbeat_at = ?, result_code = ?,
                            result_json = ?, progress_code = NULL
                        WHERE job_id = ? AND state = 'QUEUED'
                        """,
                        (
                            terminal_state.value,
                            current,
                            current,
                            result.stable_code.value,
                            payload,
                            job.job_id,
                        ),
                    )
                    if updated.rowcount != 1:
                        raise BrokerDatabaseError(
                            "queued Job restart transition lost its authority"
                        )
                    self.events.record_terminal_in_transaction(
                        connection,
                        job,
                        state=terminal_state,
                        stable_code=result.stable_code,
                        occurred_at=current,
                    )
                    terminalized_job_ids.append(job.job_id)
                if profile_broken:
                    connection.execute(
                        """
                        UPDATE account_state
                        SET state = 'PROFILE_BROKEN', next_job_at = 0,
                            blocked_reason = 'PROFILE_BROKEN',
                            updated_at = ?
                        WHERE account_scope = ?
                        """,
                        (current, ACCOUNT_SCOPE),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE account_state
                        SET state = 'AUTH_REQUIRED', next_job_at = 0,
                            blocked_reason = 'AUTHENTICATION_REQUIRED',
                            updated_at = ?
                        WHERE account_scope = ?
                          AND state NOT IN (
                              'RATE_LIMITED', 'RISK_CONTROL',
                              'PROFILE_BROKEN', 'RECONCILIATION_REQUIRED'
                          )
                        """,
                        (current, ACCOUNT_SCOPE),
                    )
        except (sqlite3.Error, TypeError, ValueError) as error:
            if isinstance(error, BrokerDatabaseError):
                raise
            raise BrokerDatabaseError("Owner restart transition failed") from error
        for job_id in terminalized_job_ids:
            self.cleanup_after_terminal(job_id)
        return len(terminalized_job_ids)

    def acknowledge_uncertain_completion(
        self,
        job: Job,
        *,
        contract_version: int,
        current: float,
    ) -> None:
        result = ExecutorResult(stable_code=StableCode.PUBLISH_OUTCOME_UNKNOWN_ACKNOWLEDGED)
        payload = result_json(
            job=job,
            status=JobState.FAILED,
            result=result,
            completed_at=current,
            operation_contract_version=contract_version,
        )
        with self.database.immediate() as connection:
            account = connection.execute(
                """
                SELECT state, next_job_at, blocked_reason
                FROM account_state
                WHERE account_scope = ?
                """,
                (ACCOUNT_SCOPE,),
            ).fetchone()
            if account is None:
                raise BrokerDatabaseError("primary account state is missing")
            current_account_state = AccountState(str(account["state"]))
            current_blocked_reason = (
                str(account["blocked_reason"]) if account["blocked_reason"] is not None else None
            )
            if (
                current_account_state is AccountState.RECONCILIATION_REQUIRED
                and current_blocked_reason != _UNKNOWN_RESULT_CODE
            ):
                raise BrokerDatabaseError("uncertain account state is inconsistent")
            profile_broken = (
                current_account_state is AccountState.PROFILE_BROKEN
                or self.profile_state_is_broken(connection)
            )
            if profile_broken:
                final_account_state = AccountState.PROFILE_BROKEN
                final_next_job_at = 0.0
                final_blocked_reason = StableCode.PROFILE_BROKEN.value
            elif current_account_state is AccountState.RECONCILIATION_REQUIRED:
                final_account_state = AccountState.HEALTHY
                final_next_job_at = 0.0
                final_blocked_reason = None
            else:
                final_account_state = current_account_state
                final_next_job_at = float(account["next_job_at"])
                final_blocked_reason = current_blocked_reason
            updated = connection.execute(
                """
                UPDATE jobs
                SET finished_at = ?, heartbeat_at = ?, result_code = ?,
                    result_json = ?, progress_code = NULL
                WHERE job_id = ? AND state = 'FAILED'
                  AND result_code = 'PUBLISH_OUTCOME_UNKNOWN'
                """,
                (
                    current,
                    current,
                    _ACKNOWLEDGED_RESULT_CODE,
                    payload,
                    job.job_id,
                ),
            )
            if updated.rowcount != 1:
                raise BrokerDatabaseError("uncertain acknowledgement lost its Job authority")
            account_updated = connection.execute(
                """
                UPDATE account_state
                SET state = ?, next_job_at = ?,
                    blocked_reason = ?, updated_at = ?
                WHERE account_scope = ?
                """,
                (
                    final_account_state.value,
                    final_next_job_at,
                    final_blocked_reason,
                    current,
                    ACCOUNT_SCOPE,
                ),
            )
            if account_updated.rowcount != 1:
                raise BrokerDatabaseError("uncertain acknowledgement lost its account authority")
            self.events.record_job_event_in_transaction(
                connection,
                OperationalEventType.PUBLISH_ACKNOWLEDGED,
                job,
                occurred_at=current,
                stable_code=result.stable_code,
            )

    def require_login_after_owner_stop(self) -> None:
        """Record that a new browser process cannot inherit Session authority."""

        try:
            with self.database.immediate() as connection:
                connection.execute(
                    """
                    UPDATE account_state
                    SET state = 'AUTH_REQUIRED', next_job_at = 0,
                        blocked_reason = 'AUTHENTICATION_REQUIRED',
                        updated_at = ?
                    WHERE account_scope = ?
                      AND state NOT IN (
                          'RATE_LIMITED', 'RISK_CONTROL',
                          'PROFILE_BROKEN', 'RECONCILIATION_REQUIRED'
                      )
                    """,
                    (time.time(), ACCOUNT_SCOPE),
                )
        except sqlite3.Error as error:
            raise BrokerDatabaseError("Owner stop login transition failed") from error

    @staticmethod
    def row_to_job(row: sqlite3.Row) -> Job:
        from pathlib import Path

        return Job(
            job_id=str(row["job_id"]),
            account_scope=str(row["account_scope"]),
            game=str(row["game"]),
            operation=str(row["operation"]),
            repo_root=Path(str(row["repo_root"])),
            dedupe_key=str(row["dedupe_key"]),
            parameter_digest=str(row["parameter_digest"]),
            force=bool(row["force"]),
            state=JobState(str(row["state"])),
            created_at=float(row["created_at"]),
            not_before_at=float(row["not_before_at"]),
            started_at=(float(row["started_at"]) if row["started_at"] is not None else None),
        )
