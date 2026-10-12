"""SQLite WAL storage, schema initialization, and monotonic migrations."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import stat
import time
from collections.abc import Iterator

from .config import ACCOUNT_SCOPE, Settings
from .errors import BrokerDatabaseError, ExternalStateUnsafe
from .locks import (
    FileLock,
    ensure_private_directory,
    ensure_private_file,
    ensure_safe_parent_directory,
)
from .parameter_fingerprint import ensure_parameter_hmac_key

SCHEMA_VERSION = 7

_ACCOUNT_STATE_V3 = f"""
CREATE TABLE account_state (
    account_scope TEXT PRIMARY KEY CHECK (account_scope = '{ACCOUNT_SCOPE}'),
    state TEXT NOT NULL CHECK (
        state IN (
            'HEALTHY', 'COOLDOWN', 'AUTH_REQUIRED', 'RATE_LIMITED',
            'RISK_CONTROL', 'PROFILE_BROKEN', 'RECONCILIATION_REQUIRED'
        )
    ),
    next_job_at REAL NOT NULL DEFAULT 0,
    blocked_reason TEXT,
    last_success_game TEXT,
    last_success_at REAL,
    updated_at REAL NOT NULL
);
"""

_PROFILE_STATE_V3 = """
CREATE TABLE profile_state (
    profile_id TEXT PRIMARY KEY,
    account_scope TEXT NOT NULL,
    path TEXT NOT NULL,
    browser_channel TEXT NOT NULL,
    state TEXT NOT NULL,
    expected_account_hash TEXT,
    last_verified_at REAL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (account_scope) REFERENCES account_state(account_scope)
);
"""
_PROFILE_STATE_V4 = """
CREATE TABLE profile_state (
    profile_id TEXT PRIMARY KEY,
    account_scope TEXT NOT NULL,
    browser_channel TEXT NOT NULL,
    state TEXT NOT NULL,
    expected_account_hash TEXT,
    last_verified_at REAL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (account_scope) REFERENCES account_state(account_scope)
);
"""
_LOGIN_IDENTITY_STATE_V5 = """
CREATE TABLE login_identity_state (
    game TEXT PRIMARY KEY CHECK (game IN ('genshin', 'zzz', 'hsr')),
    identity_fingerprint TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source = 'VERIFIED_BINDING'),
    verified_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""
_ACCOUNT_STATE_CURRENT = _ACCOUNT_STATE_V3.replace(
    "CREATE TABLE account_state",
    "CREATE TABLE IF NOT EXISTS account_state",
)
_PROFILE_STATE_CURRENT = _PROFILE_STATE_V4.replace(
    "CREATE TABLE profile_state",
    "CREATE TABLE IF NOT EXISTS profile_state",
)
_LOGIN_IDENTITY_STATE_CURRENT = _LOGIN_IDENTITY_STATE_V5.replace(
    "CREATE TABLE login_identity_state",
    "CREATE TABLE IF NOT EXISTS login_identity_state",
)
_OPERATIONAL_EVENTS_V7 = """
CREATE TABLE IF NOT EXISTS operational_events (
    event_id INTEGER PRIMARY KEY,
    occurred_at REAL NOT NULL CHECK (occurred_at >= 0),
    event_type TEXT NOT NULL CHECK (
        event_type IN (
            'OWNER_STARTING', 'OWNER_READY', 'OWNER_STOPPING', 'OWNER_STOPPED',
            'OWNER_BROKEN', 'OWNER_RECOVERED', 'CONTEXT_CREATED',
            'CONTEXT_INVALIDATED', 'JOB_SUBMITTED', 'JOB_COALESCED',
            'JOB_REUSED', 'JOB_CLAIMED', 'JOB_SUCCEEDED', 'JOB_FAILED',
            'JOB_BLOCKED', 'JOB_ABANDONED', 'AUTHENTICATION_REQUIRED',
            'RATE_LIMITED', 'RISK_CONTROL', 'PROFILE_BROKEN',
            'RUNTIME_MISMATCH', 'PUBLISH_OUTCOME_UNKNOWN',
            'PUBLISH_RECONCILED', 'PUBLISH_ACKNOWLEDGED',
            'RETENTION_MAINTENANCE_FAILED',
            'RETENTION_MAINTENANCE_RECOVERED'
        )
    ),
    game TEXT CHECK (game IS NULL OR game IN ('genshin', 'zzz', 'hsr', 'session')),
    operation TEXT,
    job_id TEXT,
    stable_code TEXT,
    diagnostic_hash TEXT CHECK (
        diagnostic_hash IS NULL OR (
            length(diagnostic_hash) = 64
            AND diagnostic_hash NOT GLOB '*[^0-9a-f]*'
        )
    )
);

CREATE INDEX IF NOT EXISTS operational_events_recent_idx
ON operational_events(occurred_at DESC, event_id DESC);
"""
_MAINTENANCE_STATE_V7 = """
CREATE TABLE IF NOT EXISTS maintenance_state (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    retention_state TEXT NOT NULL CHECK (
        retention_state IN ('HEALTHY', 'FAILED')
    ),
    retention_diagnostic_hash TEXT CHECK (
        retention_diagnostic_hash IS NULL OR (
            length(retention_diagnostic_hash) = 64
            AND retention_diagnostic_hash NOT GLOB '*[^0-9a-f]*'
        )
    ),
    updated_at REAL NOT NULL CHECK (updated_at >= 0),
    CHECK (
        (retention_state = 'HEALTHY' AND retention_diagnostic_hash IS NULL)
        OR
        (retention_state = 'FAILED' AND retention_diagnostic_hash IS NOT NULL)
    )
);
"""

_SCHEMA_CURRENT = f"""
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    account_scope TEXT NOT NULL CHECK (account_scope = '{ACCOUNT_SCOPE}'),
    game TEXT NOT NULL,
    operation TEXT NOT NULL,
    repo_root TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    parameter_digest TEXT NOT NULL,
    force INTEGER NOT NULL DEFAULT 0 CHECK (force IN (0, 1)),
    state TEXT NOT NULL CHECK (
        state IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'BLOCKED', 'ABANDONED')
    ),
    created_at REAL NOT NULL,
    not_before_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    worker_id TEXT,
    heartbeat_at REAL,
    result_code TEXT,
    result_json TEXT,
    progress_code TEXT
);

CREATE INDEX IF NOT EXISTS jobs_dispatch_idx
ON jobs(state, not_before_at, created_at, job_id);

CREATE INDEX IF NOT EXISTS jobs_dedupe_history_idx
ON jobs(dedupe_key, finished_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS jobs_active_dedupe_idx
ON jobs(dedupe_key)
WHERE state IN ('QUEUED', 'RUNNING');

{_ACCOUNT_STATE_CURRENT}

CREATE TABLE IF NOT EXISTS rate_state (
    scope_key TEXT PRIMARY KEY,
    next_allowed_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);

{_PROFILE_STATE_CURRENT}

{_LOGIN_IDENTITY_STATE_CURRENT}

{_OPERATIONAL_EVENTS_V7}

{_MAINTENANCE_STATE_V7}

CREATE TABLE IF NOT EXISTS worker_state (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    worker_id TEXT,
    pid INTEGER,
    heartbeat_at REAL,
    runtime_digest TEXT,
    owner_phase TEXT NOT NULL DEFAULT 'STOPPED' CHECK (
        owner_phase IN ('STARTING', 'RUNNING', 'STOPPING', 'BROKEN', 'STOPPED')
    ),
    browser_context_ready INTEGER NOT NULL DEFAULT 0 CHECK (
        browser_context_ready IN (0, 1)
    ),
    context_generation TEXT
);
"""


class Database:
    """The only authority for the fixed broker database path."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = settings.database_file

    def _prepare_layout(self) -> None:
        ensure_safe_parent_directory(self.settings.root)
        ensure_private_directory(self.settings.broker_root)
        ensure_private_directory(self.settings.broker_tmp)
        ensure_parameter_hmac_key(self.settings)
        ensure_private_file(self.path)
        self._validate_existing_sidecars()

    def _connect(self) -> sqlite3.Connection:
        self._prepare_layout()
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=self.settings.sqlite_busy_timeout_ms / 1000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {self.settings.sqlite_busy_timeout_ms}")
            connection.execute("PRAGMA foreign_keys = ON")
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()
            if mode is None or str(mode[0]).lower() != "wal":
                connection.close()
                raise BrokerDatabaseError("SQLite WAL mode could not be enabled")
            return connection
        except (sqlite3.Error, OSError, ExternalStateUnsafe) as error:
            if isinstance(error, BrokerDatabaseError):
                raise
            raise BrokerDatabaseError("broker database could not be opened") from error

    def _connect_readonly(self) -> sqlite3.Connection:
        path = self.path.expanduser().absolute()
        try:
            metadata = path.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise BrokerDatabaseError("broker database type, owner, or permissions are unsafe")
            self._validate_existing_sidecars()
            connection = sqlite3.connect(
                f"{path.as_uri()}?mode=ro",
                uri=True,
                timeout=self.settings.sqlite_busy_timeout_ms / 1000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {self.settings.sqlite_busy_timeout_ms}")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA query_only = ON")
            return connection
        except BrokerDatabaseError:
            raise
        except (sqlite3.Error, OSError) as error:
            raise BrokerDatabaseError("broker database could not be opened read-only") from error

    @contextlib.contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()
            self._secure_sidecars()

    @contextlib.contextmanager
    def readonly_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect_readonly()
        try:
            yield connection
        finally:
            connection.close()

    @contextlib.contextmanager
    def immediate(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
                raise

    def initialize(self) -> None:
        self._prepare_layout()
        lock = FileLock(self.settings.database_init_lock_file)
        lock.acquire(blocking=True)
        try:
            self._initialize_locked()
        finally:
            lock.release()

    def _initialize_locked(self) -> None:
        try:
            with self.connection() as connection:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                if "schema_meta" not in tables:
                    connection.executescript(
                        f"""
                        BEGIN IMMEDIATE;
                        {_SCHEMA_CURRENT}
                        INSERT INTO schema_meta(key, value)
                        VALUES('schema_version', '{SCHEMA_VERSION}')
                        ON CONFLICT(key) DO NOTHING;
                        COMMIT;
                        """
                    )
                    row = connection.execute(
                        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                    ).fetchone()
                    if row is None or str(row[0]) != str(SCHEMA_VERSION):
                        raise BrokerDatabaseError("broker schema initialization lost its authority")
                else:
                    row = connection.execute(
                        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                    ).fetchone()
                    if row is None:
                        raise BrokerDatabaseError("broker schema version is missing")
                    try:
                        version = int(row[0])
                    except (TypeError, ValueError) as error:
                        raise BrokerDatabaseError("broker schema version is invalid") from error
                    if version > SCHEMA_VERSION:
                        raise BrokerDatabaseError("broker schema is newer than this package")
                    if version < 0:
                        raise BrokerDatabaseError("broker schema version is invalid")
                    while version < SCHEMA_VERSION:
                        version = self._migrate(connection, version)
                now = time.time()
                connection.execute(
                    """
                    INSERT INTO account_state(
                        account_scope, state, next_job_at, blocked_reason,
                        last_success_game, last_success_at, updated_at
                    ) VALUES(?, 'HEALTHY', 0, NULL, NULL, NULL, ?)
                    ON CONFLICT(account_scope) DO NOTHING
                    """,
                    (ACCOUNT_SCOPE, now),
                )
                connection.execute(
                    """
                    INSERT INTO maintenance_state(
                        singleton_id, retention_state,
                        retention_diagnostic_hash, updated_at
                    ) VALUES(1, 'HEALTHY', NULL, ?)
                    ON CONFLICT(singleton_id) DO NOTHING
                    """,
                    (now,),
                )
                connection.execute(
                    """
                    INSERT INTO worker_state(
                        singleton_id, worker_id, pid, heartbeat_at, runtime_digest,
                        owner_phase, browser_context_ready, context_generation
                    ) VALUES(1, NULL, NULL, NULL, NULL, 'STOPPED', 0, NULL)
                    ON CONFLICT(singleton_id) DO NOTHING
                    """
                )
        except BrokerDatabaseError:
            raise
        except (sqlite3.Error, OSError, ValueError) as error:
            raise BrokerDatabaseError("broker schema initialization failed") from error

    def _migrate(self, connection: sqlite3.Connection, version: int) -> int:
        try:
            if version == 0:
                target_version = SCHEMA_VERSION
                connection.executescript(
                    f"""
                    BEGIN IMMEDIATE;
                    {_SCHEMA_CURRENT}
                    UPDATE schema_meta
                    SET value = '{SCHEMA_VERSION}'
                    WHERE key = 'schema_version' AND value = '0';
                    COMMIT;
                    """
                )
            elif version == 1:
                target_version = 2
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    ALTER TABLE jobs
                    ADD COLUMN parameter_digest TEXT NOT NULL DEFAULT
                    'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855';
                    ALTER TABLE jobs ADD COLUMN progress_code TEXT;
                    UPDATE schema_meta
                    SET value = '2'
                    WHERE key = 'schema_version' AND value = '1';
                    COMMIT;
                    """
                )
            elif version == 2:
                target_version = 3
                connection.executescript(
                    f"""
                    PRAGMA foreign_keys = OFF;
                    BEGIN IMMEDIATE;
                    ALTER TABLE profile_state RENAME TO profile_state_v2;
                    ALTER TABLE account_state RENAME TO account_state_v2;
                    {_ACCOUNT_STATE_V3}
                    {_PROFILE_STATE_V3}
                    INSERT INTO account_state(
                        account_scope, state, next_job_at, blocked_reason,
                        last_success_game, last_success_at, updated_at
                    )
                    SELECT
                        account_scope, state, next_job_at, blocked_reason,
                        last_success_game, last_success_at, updated_at
                    FROM account_state_v2;
                    INSERT INTO profile_state(
                        profile_id, account_scope, path, browser_channel,
                        state, expected_account_hash, last_verified_at, updated_at
                    )
                    SELECT
                        profile_id, account_scope, path, browser_channel,
                        state, expected_account_hash, last_verified_at, updated_at
                    FROM profile_state_v2;
                    DROP TABLE profile_state_v2;
                    DROP TABLE account_state_v2;
                    UPDATE schema_meta
                    SET value = '3'
                    WHERE key = 'schema_version' AND value = '2';
                    COMMIT;
                    PRAGMA foreign_keys = ON;
                    """
                )
            elif version == 3:
                target_version = 4
                connection.executescript(
                    f"""
                    PRAGMA foreign_keys = OFF;
                    BEGIN IMMEDIATE;
                    ALTER TABLE profile_state RENAME TO profile_state_v3;
                    {_PROFILE_STATE_V4}
                    INSERT INTO profile_state(
                        profile_id, account_scope, browser_channel,
                        state, expected_account_hash, last_verified_at, updated_at
                    )
                    SELECT
                        profile_id, account_scope, browser_channel,
                        state, expected_account_hash, last_verified_at, updated_at
                    FROM profile_state_v3;
                    DROP TABLE profile_state_v3;
                    UPDATE schema_meta
                    SET value = '4'
                    WHERE key = 'schema_version' AND value = '3';
                    COMMIT;
                    PRAGMA foreign_keys = ON;
                    """
                )
            elif version == 4:
                target_version = 5
                connection.executescript(
                    f"""
                    BEGIN IMMEDIATE;
                    {_LOGIN_IDENTITY_STATE_V5}
                    UPDATE schema_meta
                    SET value = '5'
                    WHERE key = 'schema_version' AND value = '4';
                    COMMIT;
                    """
                )
            elif version == 5:
                target_version = 6
                columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(worker_state)")
                }
                connection.execute("BEGIN IMMEDIATE")
                if "owner_phase" not in columns:
                    connection.execute(
                        """
                        ALTER TABLE worker_state
                        ADD COLUMN owner_phase TEXT NOT NULL DEFAULT 'STOPPED'
                        CHECK (
                            owner_phase IN (
                                'STARTING', 'RUNNING', 'STOPPING', 'BROKEN', 'STOPPED'
                            )
                        )
                        """
                    )
                if "browser_context_ready" not in columns:
                    connection.execute(
                        """
                        ALTER TABLE worker_state
                        ADD COLUMN browser_context_ready INTEGER NOT NULL DEFAULT 0
                        CHECK (browser_context_ready IN (0, 1))
                        """
                    )
                if "context_generation" not in columns:
                    connection.execute(
                        "ALTER TABLE worker_state ADD COLUMN context_generation TEXT"
                    )
                connection.execute(
                    """
                    UPDATE schema_meta
                    SET value = '6'
                    WHERE key = 'schema_version' AND value = '5'
                    """
                )
                connection.execute("COMMIT")
            elif version == 6:
                target_version = 7
                connection.executescript(
                    f"""
                    BEGIN IMMEDIATE;
                    {_OPERATIONAL_EVENTS_V7}
                    {_MAINTENANCE_STATE_V7}
                    INSERT INTO maintenance_state(
                        singleton_id, retention_state,
                        retention_diagnostic_hash, updated_at
                    ) VALUES(1, 'HEALTHY', NULL, unixepoch())
                    ON CONFLICT(singleton_id) DO NOTHING;
                    UPDATE schema_meta
                    SET value = '7'
                    WHERE key = 'schema_version' AND value = '6';
                    COMMIT;
                    """
                )
            else:
                raise BrokerDatabaseError("broker schema migration path is unavailable")
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None or str(row[0]) != str(target_version):
                raise BrokerDatabaseError("broker schema migration lost its authority")
        except sqlite3.Error as error:
            raise BrokerDatabaseError("broker schema migration failed") from error
        return target_version

    def schema_status(self) -> tuple[int, str, bool]:
        self.initialize()
        try:
            with self.connection() as connection:
                row = connection.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()
                mode = connection.execute("PRAGMA journal_mode").fetchone()
                foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()
                if row is None or mode is None or foreign_keys is None:
                    raise BrokerDatabaseError("broker schema status is incomplete")
                return int(row[0]), str(mode[0]).lower(), bool(foreign_keys[0])
        except (sqlite3.Error, TypeError, ValueError) as error:
            if isinstance(error, BrokerDatabaseError):
                raise
            raise BrokerDatabaseError("broker schema status could not be read") from error

    def readonly_schema_status(self) -> tuple[int, str, bool]:
        try:
            with self.readonly_connection() as connection:
                row = connection.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()
                mode = connection.execute("PRAGMA journal_mode").fetchone()
                foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()
                if row is None or mode is None or foreign_keys is None:
                    raise BrokerDatabaseError("broker schema status is incomplete")
                return int(row[0]), str(mode[0]).lower(), bool(foreign_keys[0])
        except (sqlite3.Error, TypeError, ValueError) as error:
            if isinstance(error, BrokerDatabaseError):
                raise
            raise BrokerDatabaseError("broker schema status could not be read") from error

    def _validate_existing_sidecars(self) -> None:
        for suffix in ("-wal", "-shm"):
            path = self.path.with_name(self.path.name + suffix)
            if not os.path.lexists(path):
                continue
            try:
                metadata = path.lstat()
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                ):
                    raise BrokerDatabaseError(
                        "SQLite sidecar type, owner, or permissions are unsafe"
                    )
            except FileNotFoundError:
                continue
            except OSError as error:
                raise BrokerDatabaseError("SQLite sidecar permissions are unsafe") from error

    def _secure_sidecars(self) -> None:
        self._validate_existing_sidecars()
