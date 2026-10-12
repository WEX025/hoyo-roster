"""FIFO claim, account block, and running-transition authority."""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from .config import ACCOUNT_SCOPE, LOGIN_OPERATION, SESSION_GAME, Settings
from .db import Database
from .errors import BrokerDatabaseError
from .execution_identity import ExecutionIdentityAuthority
from .models import (
    AccountState,
    ClaimOutcome,
    Job,
    JobState,
    StableCode,
)
from .operational_events import OperationalEventStore, OperationalEventType
from .queue_completion import CompletionAuthority

_BLOCKING_ACCOUNT_STATES = frozenset(
    {
        AccountState.AUTH_REQUIRED,
        AccountState.RATE_LIMITED,
        AccountState.RISK_CONTROL,
        AccountState.PROFILE_BROKEN,
        AccountState.RECONCILIATION_REQUIRED,
    }
)


class DispatchAuthority:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        identity: ExecutionIdentityAuthority,
        completion: CompletionAuthority,
        events: OperationalEventStore,
    ) -> None:
        self.settings = settings
        self.database = database
        self.identity = identity
        self.completion = completion
        self.events = events

    def claim_next(
        self,
        worker_id: str,
        *,
        now: float | None = None,
        runtime_digest: str | None = None,
    ) -> ClaimOutcome:
        current = time.time() if now is None else now
        execution_runtime_digest = (
            self.identity.active_runtime_digest() if runtime_digest is None else runtime_digest
        )
        try:
            with self.database.immediate() as connection:
                running = connection.execute(
                    "SELECT 1 FROM jobs WHERE state = 'RUNNING' LIMIT 1"
                ).fetchone()
                if running is not None:
                    return ClaimOutcome(job=None, state_changed=False)
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
                account_state = AccountState(str(account["state"]))
                next_job_at = float(account["next_job_at"])
                login_recovery = False
                if account_state in _BLOCKING_ACCOUNT_STATES:
                    blocked = connection.execute(
                        """
                        SELECT *
                        FROM jobs
                        WHERE state = 'QUEUED'
                        ORDER BY created_at, job_id
                        LIMIT 1
                        """
                    ).fetchone()
                    if blocked is None:
                        return ClaimOutcome(job=None, state_changed=False)
                    job = self.completion.row_to_job(blocked)
                    if not self.identity.job_matches(
                        job,
                        runtime_digest=execution_runtime_digest,
                    ):
                        self.completion.terminalize_execution_identity_mismatch(
                            connection,
                            job,
                            current=current,
                        )
                        return ClaimOutcome(job=None, state_changed=True)
                    login_recovery = (
                        account_state is AccountState.AUTH_REQUIRED
                        and job.game == SESSION_GAME
                        and job.operation == LOGIN_OPERATION
                    )
                    if not login_recovery:
                        code = self.blocked_code(
                            account_state,
                            account["blocked_reason"],
                        )
                        self.completion.block_queued_job(
                            connection,
                            job,
                            code,
                            current=current,
                        )
                        return ClaimOutcome(job=None, state_changed=True)
                if next_job_at > current and not login_recovery:
                    if account_state is not AccountState.COOLDOWN:
                        updated = connection.execute(
                            """
                            UPDATE account_state
                            SET state = 'COOLDOWN', updated_at = ?
                            WHERE account_scope = ? AND state = ?
                            """,
                            (current, ACCOUNT_SCOPE, account_state.value),
                        )
                        if updated.rowcount != 1:
                            raise BrokerDatabaseError("account cooldown transition failed")
                    return ClaimOutcome(
                        job=None,
                        state_changed=False,
                        next_check_at=next_job_at,
                    )
                if account_state is AccountState.COOLDOWN:
                    updated = connection.execute(
                        """
                        UPDATE account_state
                        SET state = 'HEALTHY', blocked_reason = NULL, updated_at = ?
                        WHERE account_scope = ? AND state = 'COOLDOWN'
                        """,
                        (current, ACCOUNT_SCOPE),
                    )
                    if updated.rowcount != 1:
                        raise BrokerDatabaseError("account cooldown expiry transition failed")
                row = connection.execute(
                    """
                    SELECT *
                    FROM jobs
                    WHERE state = 'QUEUED' AND not_before_at <= ?
                    ORDER BY created_at, job_id
                    LIMIT 1
                    """,
                    (current,),
                ).fetchone()
                if row is None:
                    future = connection.execute(
                        """
                        SELECT MIN(not_before_at)
                        FROM jobs
                        WHERE state = 'QUEUED'
                        """
                    ).fetchone()
                    future_time = (
                        float(future[0]) if future is not None and future[0] is not None else None
                    )
                    return ClaimOutcome(
                        job=None,
                        state_changed=False,
                        next_check_at=future_time,
                    )
                job = self.completion.row_to_job(row)
                if not self.identity.job_matches(
                    job,
                    runtime_digest=execution_runtime_digest,
                ):
                    self.completion.terminalize_execution_identity_mismatch(
                        connection,
                        job,
                        current=current,
                    )
                    return ClaimOutcome(job=None, state_changed=True)
                updated = connection.execute(
                    """
                    UPDATE jobs
                    SET state = 'RUNNING', started_at = ?, worker_id = ?, heartbeat_at = ?
                    WHERE job_id = ? AND state = 'QUEUED'
                    """,
                    (current, worker_id, current, job.job_id),
                )
                if updated.rowcount != 1:
                    raise BrokerDatabaseError("job claim lost its authority")
                claimed_job = Job(
                    job_id=job.job_id,
                    account_scope=job.account_scope,
                    game=job.game,
                    operation=job.operation,
                    repo_root=job.repo_root,
                    dedupe_key=job.dedupe_key,
                    parameter_digest=job.parameter_digest,
                    force=job.force,
                    state=JobState.RUNNING,
                    created_at=job.created_at,
                    not_before_at=job.not_before_at,
                    started_at=current,
                )
                self.events.record_job_event_in_transaction(
                    connection,
                    OperationalEventType.JOB_CLAIMED,
                    claimed_job,
                    occurred_at=current,
                )
                return ClaimOutcome(
                    job=claimed_job,
                    state_changed=True,
                )
        except (sqlite3.Error, ValueError, TypeError) as error:
            if isinstance(error, BrokerDatabaseError):
                raise
            raise BrokerDatabaseError("job claim failed") from error

    @staticmethod
    def blocked_code(state: AccountState, persisted: Any) -> StableCode:
        expected = {
            AccountState.AUTH_REQUIRED: StableCode.AUTHENTICATION_REQUIRED,
            AccountState.RATE_LIMITED: StableCode.RATE_LIMITED,
            AccountState.RISK_CONTROL: StableCode.RISK_CONTROL,
            AccountState.PROFILE_BROKEN: StableCode.PROFILE_BROKEN,
            AccountState.RECONCILIATION_REQUIRED: (StableCode.PUBLISH_OUTCOME_UNKNOWN),
        }[state]
        if persisted is not None and str(persisted) != expected.value:
            raise BrokerDatabaseError("account blocked reason is inconsistent")
        return expected
