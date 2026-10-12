"""Authoritative Owner health fact collection and pure classification."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .config import Settings
from .errors import WorkerStartFailed
from .locks import is_lock_held
from .models import OwnerPhase, OwnerRecord
from .queue import JobQueue
from .runtime import read_active_runtime


class OwnerState(StrEnum):
    HEALTHY = "HEALTHY"
    STARTING = "STARTING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    RUNTIME_MISMATCH = "RUNTIME_MISMATCH"
    BROKEN = "BROKEN"


@dataclass(frozen=True, slots=True)
class OwnerHealth:
    state: OwnerState
    phase: OwnerPhase
    owner_id_present: bool
    process_alive: bool
    heartbeat_fresh: bool
    worker_lock_held: bool
    profile_lock_held: bool
    browser_context_ready: bool
    context_generation_present: bool
    runtime_match: bool
    context_generation: str | None

    @property
    def healthy(self) -> bool:
        return self.state is OwnerState.HEALTHY


@dataclass(frozen=True, slots=True)
class OwnerStatus:
    state: OwnerState
    phase: OwnerPhase
    runtime_match: bool
    browser_context_ready: bool
    worker_lock_held: bool
    profile_lock_held: bool
    heartbeat_fresh: bool
    process_alive: bool
    queue_depth: int
    context_generation_present: bool
    context_generation: str | None


@dataclass(frozen=True, slots=True)
class OwnerHealthFacts:
    record: OwnerRecord
    active_runtime_digest: str
    process_alive: bool
    heartbeat_fresh: bool
    worker_lock_held: bool
    profile_lock_held: bool
    expected_context_generation: str | None = None


def runtime_digest(settings: Settings) -> str:
    if settings.test_mode:
        return "0" * 64
    marker = read_active_runtime(settings)
    digest = marker["runtime_digest"]
    if not isinstance(digest, str):
        raise WorkerStartFailed("active Runtime digest is unavailable")
    return digest


def process_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def held_if_present(path: Path) -> bool:
    return os.path.lexists(path) and is_lock_held(path)


def classify_owner_health(facts: OwnerHealthFacts) -> OwnerHealth:
    """Classify immutable Owner facts without I/O or recovery side effects."""

    record = facts.record
    owner_id_present = bool(record.owner_id)
    context_generation_present = bool(record.context_generation)
    context_generation_match = (
        facts.expected_context_generation is None
        or record.context_generation == facts.expected_context_generation
    )
    runtime_match = (
        record.runtime_digest == facts.active_runtime_digest
        if owner_id_present or record.runtime_digest is not None
        else True
    )
    fully_healthy = all(
        (
            owner_id_present,
            facts.process_alive,
            facts.heartbeat_fresh,
            facts.worker_lock_held,
            facts.profile_lock_held,
            record.phase is OwnerPhase.RUNNING,
            record.browser_context_ready,
            context_generation_present,
            context_generation_match,
            runtime_match,
        )
    )
    stopped_record = all(
        (
            record.owner_id is None,
            record.pid is None,
            record.heartbeat_at is None,
            record.runtime_digest is None,
            record.phase is OwnerPhase.STOPPED,
            not record.browser_context_ready,
            record.context_generation is None,
            not facts.worker_lock_held,
            not facts.profile_lock_held,
        )
    )
    if fully_healthy:
        state = OwnerState.HEALTHY
    elif stopped_record:
        state = OwnerState.STOPPED
    elif not runtime_match:
        state = OwnerState.RUNTIME_MISMATCH
    elif (
        record.phase is OwnerPhase.STARTING
        and owner_id_present
        and facts.process_alive
        and facts.worker_lock_held
    ) or (record.phase is OwnerPhase.STOPPED and not owner_id_present and facts.worker_lock_held):
        state = OwnerState.STARTING
    elif record.phase is OwnerPhase.STOPPING and owner_id_present:
        state = OwnerState.STOPPING
    else:
        state = OwnerState.BROKEN
    return OwnerHealth(
        state=state,
        phase=record.phase,
        owner_id_present=owner_id_present,
        process_alive=facts.process_alive,
        heartbeat_fresh=facts.heartbeat_fresh,
        worker_lock_held=facts.worker_lock_held,
        profile_lock_held=facts.profile_lock_held,
        browser_context_ready=record.browser_context_ready,
        context_generation_present=context_generation_present,
        runtime_match=runtime_match,
        context_generation=record.context_generation,
    )


def evaluate_owner_health(
    settings: Settings,
    queue: JobQueue | None = None,
    *,
    active_runtime_digest: str | None = None,
) -> OwnerHealth:
    """Collect every health fact and delegate the decision to one classifier."""

    broker = queue if queue is not None else JobQueue(settings)
    record = broker.owner_record()
    current = time.time()
    return classify_owner_health(
        OwnerHealthFacts(
            record=record,
            active_runtime_digest=(
                runtime_digest(settings) if active_runtime_digest is None else active_runtime_digest
            ),
            process_alive=process_alive(record.pid),
            heartbeat_fresh=(
                record.heartbeat_at is not None
                and record.heartbeat_at >= current - settings.worker_stale_seconds
            ),
            worker_lock_held=held_if_present(settings.worker_lock_file),
            profile_lock_held=held_if_present(settings.profile_lock_file),
        )
    )


def owner_is_healthy(
    settings: Settings,
    queue: JobQueue | None = None,
) -> bool:
    return evaluate_owner_health(settings, queue).healthy
