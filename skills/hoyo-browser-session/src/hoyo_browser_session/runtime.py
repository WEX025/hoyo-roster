"""Shared Runtime marker validation and external-state initialization."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import time
from pathlib import Path
from typing import Any

from .config import EVENT_HISTORY_LIMIT, PROFILE_ID, Settings
from .db import SCHEMA_VERSION, Database
from .errors import BrokerDatabaseError, RuntimeNotReady
from .history_retention import HistoryRetention
from .locks import ensure_private_directory, ensure_safe_parent_directory
from .maintenance_state_store import MaintenanceStateStore
from .models import OwnerPhase, OwnerRecord
from .operational_events import OperationalEventStore
from .profile import ProfileManager
from .repository_identity import RepositoryIdentityError, validate_repository_identity
from .worker_protocol import classify_protocol_inventory

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_ACTIVE_KEYS = frozenset(
    {
        "schema_version",
        "runtime_digest",
        "source_commit",
        "python_minor",
        "playwright_version",
        "platform",
        "architecture",
        "created_at",
    }
)


def initialize_external_state(settings: Settings, *, repo_root: Path | None = None) -> None:
    ensure_safe_parent_directory(settings.root)
    ensure_safe_parent_directory(settings.root / "runtimes")
    ensure_private_directory(settings.runtime_root)
    ensure_private_directory(settings.generations_root)
    ProfileManager(settings).initialize(repo_root=repo_root)
    Database(settings).initialize()


def read_active_runtime(settings: Settings) -> dict[str, Any]:
    path = settings.active_runtime_file
    try:
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise RuntimeNotReady("active Runtime marker is unsafe")
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except RuntimeNotReady:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeNotReady("active Runtime marker is unavailable") from error
    if not isinstance(payload, dict) or set(payload) != _ACTIVE_KEYS:
        raise RuntimeNotReady("active Runtime marker fields are invalid")
    if payload.get("schema_version") != 1:
        raise RuntimeNotReady("active Runtime marker version is invalid")
    digest = payload.get("runtime_digest")
    source_commit = payload.get("source_commit")
    if type(digest) is not str or _DIGEST_RE.fullmatch(digest) is None:
        raise RuntimeNotReady("active Runtime digest is invalid")
    if type(source_commit) is not str or _COMMIT_RE.fullmatch(source_commit) is None:
        raise RuntimeNotReady("active Runtime source commit is invalid")
    string_fields = ("python_minor", "playwright_version", "platform", "architecture")
    if any(type(payload.get(key)) is not str or not payload[key] for key in string_fields):
        raise RuntimeNotReady("active Runtime metadata is invalid")
    created_at = payload.get("created_at")
    if not isinstance(created_at, int | float) or isinstance(created_at, bool) or created_at < 0:
        raise RuntimeNotReady("active Runtime timestamp is invalid")
    return payload


def active_runtime_python(settings: Settings) -> Path:
    marker = read_active_runtime(settings)
    digest = marker["runtime_digest"]
    if not isinstance(digest, str):
        raise RuntimeNotReady("active Runtime digest is invalid")
    python = settings.generations_root / digest / "venv/bin/python"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise RuntimeNotReady("active Runtime Python is unavailable")
    return python


def doctor_summary(settings: Settings, *, repo_root: Path) -> dict[str, object]:
    """Collect diagnosis without initializing, reconciling, or cleaning state."""

    from .owner_health import (
        OwnerHealthFacts,
        classify_owner_health,
        held_if_present,
        process_alive,
    )

    try:
        validate_repository_identity(repo_root)
    except RepositoryIdentityError:
        repository_identity = "INVALID"
    else:
        repository_identity = "VALID"
    runtime_readiness = "READY"
    runtime_digest: str | None = None
    runtime_python_ready = False
    try:
        marker = read_active_runtime(settings)
        runtime_digest = str(marker["runtime_digest"])
        runtime_python_ready = active_runtime_python(settings).is_file()
    except RuntimeNotReady:
        runtime_readiness = "NOT_READY"

    database = Database(settings)
    retention_authority = HistoryRetention(settings, database)
    protocol_inventory = classify_protocol_inventory(settings, strict=False)
    try:
        with database.readonly_connection() as connection:
            schema_row = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            journal_row = connection.execute("PRAGMA journal_mode").fetchone()
            foreign_keys_row = connection.execute("PRAGMA foreign_keys").fetchone()
            if schema_row is None or journal_row is None or foreign_keys_row is None:
                raise BrokerDatabaseError("doctor database status was incomplete")
            version = int(schema_row[0])
            owner_row = connection.execute(
                """
                SELECT worker_id, pid, heartbeat_at, runtime_digest,
                       owner_phase, browser_context_ready, context_generation
                FROM worker_state
                WHERE singleton_id = 1
                """
            ).fetchone()
            if owner_row is None:
                raise BrokerDatabaseError("doctor Owner state was unavailable")
            record = OwnerRecord(
                owner_id=(
                    str(owner_row["worker_id"]) if owner_row["worker_id"] is not None else None
                ),
                pid=(int(owner_row["pid"]) if owner_row["pid"] is not None else None),
                heartbeat_at=(
                    float(owner_row["heartbeat_at"])
                    if owner_row["heartbeat_at"] is not None
                    else None
                ),
                runtime_digest=(
                    str(owner_row["runtime_digest"])
                    if owner_row["runtime_digest"] is not None
                    else None
                ),
                phase=OwnerPhase(str(owner_row["owner_phase"])),
                browser_context_ready=bool(owner_row["browser_context_ready"]),
                context_generation=(
                    str(owner_row["context_generation"])
                    if owner_row["context_generation"] is not None
                    else None
                ),
            )
            current = time.time()
            health = classify_owner_health(
                OwnerHealthFacts(
                    record=record,
                    active_runtime_digest=runtime_digest or "unavailable",
                    process_alive=process_alive(record.pid),
                    heartbeat_fresh=(
                        record.heartbeat_at is not None
                        and record.heartbeat_at >= current - settings.worker_stale_seconds
                    ),
                    worker_lock_held=held_if_present(settings.worker_lock_file),
                    profile_lock_held=held_if_present(settings.profile_lock_file),
                )
            )
            queue_row = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN state = 'QUEUED' THEN 1 ELSE 0 END) AS queued,
                    SUM(CASE WHEN state = 'RUNNING' THEN 1 ELSE 0 END) AS running
                FROM jobs
                """
            ).fetchone()
            account_row = connection.execute(
                """
                SELECT state
                FROM account_state
                WHERE account_scope = ?
                """,
                (settings.account_scope,),
            ).fetchone()
            profile_row = connection.execute(
                """
                SELECT state
                FROM profile_state
                WHERE profile_id = ?
                """,
                (PROFILE_ID,),
            ).fetchone()
            if queue_row is None or account_row is None:
                raise BrokerDatabaseError("doctor broker state was incomplete")
            retention = retention_authority.inspect_from_connection(
                connection,
                current=current,
                protocol_inventory=protocol_inventory,
            )
            retention_current_state = "UNAVAILABLE"
            retention_current_diagnostic: str | None = None
            retention_current_updated_at: float | None = None
            if version >= 7:
                retention_current = MaintenanceStateStore.read_from_connection(connection)
                retention_current_state = retention_current.state.value
                retention_current_diagnostic = retention_current.diagnostic_hash
                retention_current_updated_at = retention_current.updated_at
                event_count_row = connection.execute(
                    "SELECT COUNT(*) FROM operational_events"
                ).fetchone()
                if event_count_row is None:
                    raise BrokerDatabaseError("doctor event state was unavailable")
                event_count = int(event_count_row[0])
                recent_events = [
                    event.as_dict()
                    for event in OperationalEventStore.recent_from_connection(
                        connection,
                        limit=10,
                    )
                ]
            else:
                event_count = 0
                recent_events = []
    except (sqlite3.Error, TypeError, ValueError) as error:
        if isinstance(error, BrokerDatabaseError):
            raise
        raise BrokerDatabaseError("doctor database inspection failed") from error

    sqlite_residue_count = sum(
        int(os.path.lexists(settings.database_file.with_name(settings.database_file.name + suffix)))
        for suffix in ("-wal", "-shm")
    )
    return {
        "repository_identity": repository_identity,
        "runtime_readiness": runtime_readiness,
        "runtime_python_ready": runtime_python_ready,
        "sqlite_schema_version": version,
        "sqlite_schema_current": version == SCHEMA_VERSION,
        "sqlite_journal_mode": str(journal_row[0]).lower(),
        "sqlite_foreign_keys": bool(foreign_keys_row[0]),
        "owner_state": health.state.value,
        "owner_phase": health.phase.value,
        "owner_runtime_match": health.runtime_match,
        "owner_context_generation_present": health.context_generation_present,
        "owner_browser_context_ready": health.browser_context_ready,
        "queue_queued": int(queue_row["queued"] or 0),
        "queue_running": int(queue_row["running"] or 0),
        "account_state": str(account_row["state"]),
        "profile_state": (
            str(profile_row["state"]) if profile_row is not None else "UNINITIALIZED"
        ),
        "unresolved_uncertain_count": retention.uncertain_protected,
        "event_count": event_count,
        "recent_events": recent_events,
        "retention_maintenance_state": retention_current_state,
        "retention_maintenance_diagnostic_hash": retention_current_diagnostic,
        "retention_maintenance_updated_at": retention_current_updated_at,
        "protocol_residue_count": protocol_inventory.safe_entry_count,
        "protocol_safe_job_count": len(protocol_inventory.safe_job_ids),
        "protocol_unsafe_or_unclassified_count": (protocol_inventory.unsafe_or_unclassified_count),
        "sqlite_residue_count": sqlite_residue_count,
        "shutdown_marker_present": os.path.lexists(settings.owner_shutdown_file),
        "event_history_limit": EVENT_HISTORY_LIMIT,
        "event_history_bounded": event_count <= EVENT_HISTORY_LIMIT,
        "terminal_job_history_limit": retention.terminal_limit,
        "terminal_job_history_bounded": retention.bounded,
        "terminal_history_total": retention.terminal_total,
        "terminal_delivery_grace_protected": (retention.delivery_grace_protected),
        "terminal_recent_success_protected": (retention.recent_success_protected),
        "terminal_uncertain_protected": retention.uncertain_protected,
        "terminal_protocol_protected": (retention.protocol_terminal_protected),
        "terminal_invalid_protected": retention.invalid_terminal_protected,
        "terminal_protected_total": retention.protected_terminal_total,
        "terminal_eligible_count": retention.eligible_terminal_count,
        "terminal_prunable_excess_count": retention.prunable_excess_count,
        "protocol_nonterminal_count": retention.protocol_nonterminal_count,
        "protocol_unclassified_count": retention.protocol_unclassified_count,
        "terminal_result_delivery_grace_seconds": (settings.terminal_result_delivery_grace_seconds),
        "durable_protection_count": (retention.active_count + retention.protected_terminal_total),
    }
