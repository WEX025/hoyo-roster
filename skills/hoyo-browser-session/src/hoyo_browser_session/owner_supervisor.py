"""Owner process startup, status, and supervision authority."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import time
from pathlib import Path

from .config import Settings, playwright_subprocess_environment
from .errors import (
    OwnerBroken,
    OwnerRuntimeRestartRequired,
    OwnerShutdownInProgress,
    WorkerStartFailed,
)
from .locks import FileLock
from .owner_health import (
    OwnerHealth,
    OwnerState,
    OwnerStatus,
    evaluate_owner_health,
    runtime_digest,
)
from .owner_maintenance import request_owner_shutdown
from .queue import JobQueue
from .shutdown import read_shutdown_marker


def assert_owner_runtime_compatible(
    settings: Settings,
    *,
    active_runtime_digest: str,
) -> None:
    queue = JobQueue(settings)
    health = evaluate_owner_health(
        settings,
        queue,
        active_runtime_digest=active_runtime_digest,
    )
    if health.state is OwnerState.RUNTIME_MISMATCH:
        queue.record_runtime_mismatch()
        raise OwnerRuntimeRestartRequired(
            "active Owner uses another Runtime; explicit shutdown and login are required"
        )


def assert_owner_submit_admissible(
    settings: Settings,
    *,
    active_runtime_digest: str,
) -> bool:
    queue = JobQueue(settings)
    health = evaluate_owner_health(
        settings,
        queue,
        active_runtime_digest=active_runtime_digest,
    )
    if health.state in {
        OwnerState.HEALTHY,
        OwnerState.STARTING,
        OwnerState.STOPPED,
    }:
        return health.healthy
    if health.state is OwnerState.RUNTIME_MISMATCH:
        queue.record_runtime_mismatch()
    _raise_if_owner_unavailable(health)
    return False


def _owner_arguments(
    settings: Settings,
    runtime_python: Path,
    repo_root: Path,
) -> list[str]:
    arguments = [
        str(runtime_python),
        "-m",
        "hoyo_browser_session",
        "owner",
        "--repo-root",
        str(repo_root),
    ]
    if settings.test_mode:
        arguments.extend(
            [
                "--test-root",
                str(settings.root),
                "--request-interval",
                str(settings.shared_request_min_interval_seconds),
                "--cooldown",
                str(settings.post_job_cooldown_seconds),
                "--reuse-window",
                str(settings.recent_success_reuse_seconds),
                "--heartbeat",
                str(settings.worker_heartbeat_seconds),
                "--stale",
                str(settings.worker_stale_seconds),
                "--executor-delay",
                str(settings.synthetic_delay_seconds),
                "--executor-timeout",
                str(settings.executor_timeout_seconds),
            ]
        )
    return arguments


def _raise_if_owner_unavailable(health: OwnerHealth) -> None:
    if health.state is OwnerState.RUNTIME_MISMATCH:
        raise OwnerRuntimeRestartRequired(
            "active Owner uses another Runtime; explicit shutdown and login are required"
        )
    if health.state is OwnerState.STOPPING:
        raise OwnerShutdownInProgress("Owner shutdown is in progress")
    if health.state is OwnerState.BROKEN:
        raise OwnerBroken("Owner state is incomplete or broken")


def _wait_for_starting_owner(
    settings: Settings,
    queue: JobQueue,
    *,
    active_digest: str,
) -> bool:
    deadline = time.monotonic() + (
        0.25 if settings.test_mode else max(5.0, settings.worker_heartbeat_seconds * 4)
    )
    while time.monotonic() < deadline:
        if read_shutdown_marker(settings) is not None:
            raise OwnerShutdownInProgress("Owner shutdown is in progress")
        health = evaluate_owner_health(
            settings,
            queue,
            active_runtime_digest=active_digest,
        )
        if health.healthy:
            return True
        _raise_if_owner_unavailable(health)
        if health.state is not OwnerState.STARTING:
            return False
        time.sleep(0.02)
    raise WorkerStartFailed("existing Owner did not finish startup")


def ensure_owner(
    settings: Settings,
    *,
    runtime_python: Path,
    repo_root: Path,
) -> None:
    """Start at most one detached Owner and require complete browser readiness."""

    queue = JobQueue(settings)
    active_digest = runtime_digest(settings)
    if read_shutdown_marker(settings) is not None:
        raise OwnerShutdownInProgress("Owner shutdown is in progress")
    health = evaluate_owner_health(
        settings,
        queue,
        active_runtime_digest=active_digest,
    )
    if health.healthy:
        return
    _raise_if_owner_unavailable(health)
    if health.state is OwnerState.STARTING and _wait_for_starting_owner(
        settings,
        queue,
        active_digest=active_digest,
    ):
        return
    if not runtime_python.is_file() or not os.access(runtime_python, os.X_OK):
        raise WorkerStartFailed("shared Runtime Python is unavailable")
    start_lock = FileLock(settings.worker_start_lock_file)
    start_lock.acquire(blocking=True)
    try:
        if read_shutdown_marker(settings) is not None:
            raise OwnerShutdownInProgress("Owner shutdown is in progress")
        health = evaluate_owner_health(
            settings,
            queue,
            active_runtime_digest=active_digest,
        )
        if health.healthy:
            return
        _raise_if_owner_unavailable(health)
        if health.state is OwnerState.STARTING and _wait_for_starting_owner(
            settings,
            queue,
            active_digest=active_digest,
        ):
            return
        if health.state is not OwnerState.STOPPED:
            raise OwnerBroken("Owner cannot be started from its current state")
        try:
            process = subprocess.Popen(
                _owner_arguments(settings, runtime_python, repo_root),
                env=playwright_subprocess_environment(settings),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as error:
            raise WorkerStartFailed("background Owner could not be started") from error
        deadline = time.monotonic() + max(
            10.0,
            settings.worker_heartbeat_seconds * 6,
        )
        while time.monotonic() < deadline:
            if read_shutdown_marker(settings) is not None:
                raise OwnerShutdownInProgress("Owner shutdown is in progress")
            health = evaluate_owner_health(
                settings,
                queue,
                active_runtime_digest=active_digest,
            )
            if health.healthy:
                return
            _raise_if_owner_unavailable(health)
            if process.poll() is not None:
                raise WorkerStartFailed("background Owner exited during startup")
            time.sleep(min(0.05, max(settings.worker_heartbeat_seconds / 2, 0.01)))
        raise WorkerStartFailed("background Owner did not become ready")
    finally:
        start_lock.release()


def owner_status(settings: Settings) -> OwnerStatus:
    queue = JobQueue(settings)
    health = evaluate_owner_health(settings, queue)
    queued, running = queue.queue_counts()
    return OwnerStatus(
        state=health.state,
        phase=health.phase,
        runtime_match=health.runtime_match,
        browser_context_ready=health.browser_context_ready,
        worker_lock_held=health.worker_lock_held,
        profile_lock_held=health.profile_lock_held,
        heartbeat_fresh=health.heartbeat_fresh,
        process_alive=health.process_alive,
        queue_depth=queued + running,
        context_generation_present=health.context_generation_present,
        context_generation=health.context_generation,
    )


async def run_owner_launch_check(
    settings: Settings,
    *,
    repo_root: Path,
) -> None:
    """Launch and explicitly stop one network-blocked Owner lifecycle."""

    from .owner_runtime import run_owner

    task = asyncio.create_task(
        run_owner(
            settings,
            repo_root=repo_root,
            block_network=True,
            runtime_digest_override="0" * 64,
            propagate_failure=True,
        )
    )
    launch_timeout = (
        10.0 if settings.test_mode else max(75.0, settings.worker_heartbeat_seconds * 15)
    )
    deadline = time.monotonic() + launch_timeout
    while time.monotonic() < deadline:
        health = evaluate_owner_health(
            settings,
            active_runtime_digest="0" * 64,
        )
        if health.state is OwnerState.HEALTHY:
            request_owner_shutdown(settings)
            result = await task
            if result != 0:
                raise OwnerBroken("network-blocked Owner launch check failed")
            return
        if task.done():
            result = await task
            if result != 0:
                raise OwnerBroken("network-blocked Owner launch check failed")
            raise OwnerBroken("network-blocked Owner exited before reaching healthy state")
        await asyncio.sleep(0.02)
    if not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    raise OwnerBroken("network-blocked Owner launch check did not become healthy")
