"""Owner shutdown and explicit recovery authority."""

from __future__ import annotations

from .config import Settings
from .errors import OwnerBroken, OwnerQueueNotEmpty, ProtocolStateError
from .models import OwnerPhase
from .owner_health import held_if_present, process_alive
from .queue import JobQueue
from .shutdown import (
    locked_protocol,
    read_shutdown_marker,
    remove_shutdown_marker,
    write_shutdown_marker,
)


def request_owner_shutdown(settings: Settings) -> bool:
    """Create one Owner-bound marker atomically with the empty-queue check."""

    queue = JobQueue(settings)
    with locked_protocol(settings):
        marker = read_shutdown_marker(settings)
        with queue.database.immediate() as connection:
            counts = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN state = 'QUEUED' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN state = 'RUNNING' THEN 1 ELSE 0 END)
                FROM jobs
                """
            ).fetchone()
            if counts is None:
                raise OwnerBroken("Owner queue state is unavailable")
            if int(counts[0] or 0) or int(counts[1] or 0):
                raise OwnerQueueNotEmpty("Owner shutdown requires an empty queue")
            row = connection.execute(
                """
                SELECT worker_id, owner_phase
                FROM worker_state
                WHERE singleton_id = 1
                """
            ).fetchone()
            if row is None:
                raise OwnerBroken("Owner state is unavailable")
            owner_id = str(row["worker_id"]) if row["worker_id"] is not None else None
            phase = OwnerPhase(str(row["owner_phase"]))
            if marker is not None:
                if owner_id is None or marker.owner_id != owner_id:
                    raise OwnerBroken("Owner shutdown marker is stale")
                return True
            if owner_id is None and phase is OwnerPhase.STOPPED:
                return False
            if owner_id is None or phase in {OwnerPhase.BROKEN, OwnerPhase.STOPPING}:
                raise OwnerBroken("Owner shutdown state is incomplete")
            write_shutdown_marker(settings, owner_id=owner_id)
            return True


def commit_shutdown_decision(
    settings: Settings,
    queue: JobQueue,
    *,
    owner_id: str,
) -> bool:
    with locked_protocol(settings):
        marker = read_shutdown_marker(settings)
        if marker is None:
            return False
        if marker.owner_id != owner_id:
            raise ProtocolStateError("Owner shutdown marker identity changed")
        return queue.commit_owner_stop_if_idle(owner_id)


def recover_stale_shutdown(
    settings: Settings,
    *,
    confirmed_owner_stopped: bool,
) -> None:
    """Explicitly clear a marker only after proving the old Owner is gone."""

    if not confirmed_owner_stopped:
        raise OwnerBroken("stale shutdown recovery requires explicit confirmation")
    queue = JobQueue(settings)
    with locked_protocol(settings):
        marker = read_shutdown_marker(settings)
        if marker is None:
            raise OwnerBroken("stale shutdown marker does not exist")
        record = queue.owner_record()
        queued, running = queue.queue_counts()
        if queued or running:
            raise OwnerQueueNotEmpty("stale shutdown recovery requires an empty queue")
        if (
            process_alive(record.pid)
            or held_if_present(settings.worker_lock_file)
            or held_if_present(settings.profile_lock_file)
        ):
            raise OwnerBroken("stale shutdown recovery found active Owner authority")
        if record.owner_id not in {None, marker.owner_id}:
            raise OwnerBroken("stale shutdown marker identity is inconsistent")
        if record.owner_id is not None:
            if record.phase not in {OwnerPhase.STOPPING, OwnerPhase.BROKEN}:
                queue.mark_owner_broken(record.owner_id)
            queue.clear_worker(record.owner_id)
        remove_shutdown_marker(settings, owner_id=marker.owner_id)


def recover_broken_owner(
    settings: Settings,
    *,
    confirmed_owner_stopped: bool,
) -> None:
    """Clear only a proven inactive BROKEN Owner record."""

    if not confirmed_owner_stopped:
        raise OwnerBroken("broken Owner recovery requires explicit confirmation")
    queue = JobQueue(settings)
    with locked_protocol(settings):
        if read_shutdown_marker(settings) is not None:
            raise OwnerBroken("broken Owner recovery cannot bypass a shutdown marker")
        record = queue.owner_record()
        queued, running = queue.queue_counts()
        if queued or running:
            raise OwnerQueueNotEmpty("broken Owner recovery requires an empty queue")
        if record.owner_id is None or record.phase is not OwnerPhase.BROKEN:
            raise OwnerBroken("broken Owner recovery requires a BROKEN Owner record")
        if (
            process_alive(record.pid)
            or held_if_present(settings.worker_lock_file)
            or held_if_present(settings.profile_lock_file)
        ):
            raise OwnerBroken("broken Owner recovery found active Owner authority")
        queue.clear_worker(record.owner_id)
