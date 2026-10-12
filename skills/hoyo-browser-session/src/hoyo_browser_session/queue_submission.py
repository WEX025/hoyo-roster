"""Request admission, dedupe, and recent-success reuse authority."""

from __future__ import annotations

import contextlib
import sqlite3
import time
import uuid
from pathlib import Path

from .config import ACCOUNT_SCOPE, Settings
from .db import Database
from .errors import (
    BrokerDatabaseError,
    ConfigurationError,
    OwnerShutdownInProgress,
    ProtocolStateError,
)
from .execution_identity import ExecutionIdentityAuthority
from .models import (
    JobParameters,
    JobResult,
    Submission,
    SubmitDisposition,
)
from .operational_events import (
    OperationalEvent,
    OperationalEventStore,
    OperationalEventType,
)
from .parameter_fingerprint import (
    empty_parameter_fingerprint,
    parameter_fingerprint,
)
from .shutdown import locked_protocol, read_shutdown_marker
from .worker_protocol import cleanup_job_protocol_files, write_job_request


class SubmissionAuthority:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        identity: ExecutionIdentityAuthority,
        events: OperationalEventStore,
    ) -> None:
        self.settings = settings
        self.database = database
        self.identity = identity
        self.events = events

    def _job_exists(self, job_id: str) -> bool:
        try:
            with self.database.connection() as connection:
                row = connection.execute(
                    "SELECT 1 FROM jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
            return row is not None
        except sqlite3.Error as error:
            raise BrokerDatabaseError("job existence could not be checked") from error

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
        root, adapter, dedupe_key = self.identity.validate_request(
            game,
            operation,
            repo_root,
        )
        if adapter is not None:
            if parameters is None:
                raise ConfigurationError("Owner job parameters are required")
            adapter.validate_parameters(parameters)
        parameter_digest = (
            parameter_fingerprint(self.settings, parameters)
            if parameters is not None
            else empty_parameter_fingerprint(self.settings)
        )
        current = time.time() if now is None else now
        request_job_id: str | None = None
        with locked_protocol(self.settings):
            if read_shutdown_marker(self.settings) is not None:
                raise OwnerShutdownInProgress(
                    "Owner shutdown is in progress; submit after it fully exits"
                )
            try:
                with self.database.immediate() as connection:
                    active = connection.execute(
                        """
                        SELECT job_id, parameter_digest
                        FROM jobs
                        WHERE dedupe_key = ? AND state IN ('QUEUED', 'RUNNING')
                        ORDER BY created_at, job_id
                        LIMIT 1
                        """,
                        (dedupe_key,),
                    ).fetchone()
                    if active is not None:
                        if str(active["parameter_digest"]) != parameter_digest:
                            raise ConfigurationError("active duplicate job parameters do not match")
                        active_job_id = str(active["job_id"])
                        self.events.record_in_transaction(
                            connection,
                            OperationalEvent(
                                occurred_at=current,
                                event_type=OperationalEventType.JOB_COALESCED,
                                game=game,
                                operation=operation,
                                job_id=active_job_id,
                                stable_code=None,
                                diagnostic_hash=None,
                            ),
                        )
                        return Submission(
                            job_id=active_job_id,
                            disposition=SubmitDisposition.COALESCED,
                        )
                    if not force:
                        reusable = connection.execute(
                            """
                            SELECT job_id, result_json
                            FROM jobs
                            WHERE dedupe_key = ?
                              AND parameter_digest = ?
                              AND state = 'SUCCEEDED'
                              AND finished_at >= ?
                              AND result_json IS NOT NULL
                            ORDER BY finished_at DESC, job_id DESC
                            LIMIT 1
                            """,
                            (
                                dedupe_key,
                                parameter_digest,
                                current - self.settings.recent_success_reuse_seconds,
                            ),
                        ).fetchone()
                        if reusable is not None:
                            persisted = JobResult.from_json(
                                str(reusable["result_json"]),
                                disposition=SubmitDisposition.REUSED,
                            )
                            self.identity.validate_persisted_result(persisted)
                            reusable_job_id = str(reusable["job_id"])
                            self.events.record_in_transaction(
                                connection,
                                OperationalEvent(
                                    occurred_at=current,
                                    event_type=OperationalEventType.JOB_REUSED,
                                    game=game,
                                    operation=operation,
                                    job_id=reusable_job_id,
                                    stable_code=persisted.stable_code,
                                    diagnostic_hash=None,
                                ),
                            )
                            return Submission(
                                job_id=reusable_job_id,
                                disposition=SubmitDisposition.REUSED,
                                reused_result=persisted,
                            )
                    job_id = str(uuid.uuid4())
                    if adapter is not None:
                        assert parameters is not None
                        write_job_request(
                            self.settings,
                            job_id=job_id,
                            game=game,
                            operation=operation,
                            parameters=parameters,
                        )
                        request_job_id = job_id
                    connection.execute(
                        """
                        INSERT INTO jobs(
                            job_id, account_scope, game, operation, repo_root,
                            dedupe_key, parameter_digest, force, state,
                            created_at, not_before_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'QUEUED', ?, ?)
                        """,
                        (
                            job_id,
                            ACCOUNT_SCOPE,
                            game,
                            operation,
                            str(root),
                            dedupe_key,
                            parameter_digest,
                            int(force),
                            current,
                            current,
                        ),
                    )
                    self.events.record_in_transaction(
                        connection,
                        OperationalEvent(
                            occurred_at=current,
                            event_type=OperationalEventType.JOB_SUBMITTED,
                            game=game,
                            operation=operation,
                            job_id=job_id,
                            stable_code=None,
                            diagnostic_hash=None,
                        ),
                    )
                    return Submission(
                        job_id=job_id,
                        disposition=SubmitDisposition.CREATED,
                    )
            except ConfigurationError:
                raise
            except (OSError, sqlite3.Error, ValueError) as error:
                if request_job_id is not None and not self._job_exists(request_job_id):
                    with contextlib.suppress(OSError, ProtocolStateError):
                        cleanup_job_protocol_files(
                            self.settings,
                            request_job_id,
                        )
                raise BrokerDatabaseError("job submission failed") from error
