"""Long-lived Playwright context and fail-closed Job execution authority."""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum
from pathlib import Path
from typing import cast

from playwright.async_api import BrowserContext, Page, Playwright

from ._owner_browser import OwnerBrowserSession, persistent_owner_browser
from .browser import BrowserJobLease, seal_context_pages
from .config import SYNTHETIC_OPERATIONS, Settings
from .errors import (
    ContextNotReusable,
    OwnerBroken,
    OwnerQueueNotEmpty,
    ProfileBroken,
    ProtocolStateError,
)
from .executors import execute_registered
from .locks import FileLock
from .models import ExecutorResult, StableCode
from .operation import BrowserAuthority
from .operation_registry import operation_adapter
from .owner_health import runtime_digest
from .owner_maintenance import commit_shutdown_decision
from .profile import ProfileManager
from .queue import JobQueue
from .request_gate import RequestGate
from .shutdown import locked_protocol, read_shutdown_marker, remove_shutdown_marker


class ContextDisposition(StrEnum):
    REUSABLE = "CONTEXT_REUSABLE"
    RESTART_REQUIRED = "CONTEXT_RESTART_REQUIRED"


_REUSABLE_JOB_CODES = frozenset(
    {
        StableCode.PASS,
        StableCode.EXECUTOR_NOT_REGISTERED,
        StableCode.AUTHENTICATION_REQUIRED,
        StableCode.LOGIN_TIMEOUT,
        StableCode.LOGIN_NETWORK_ERROR,
        StableCode.LOGIN_BINDING_FAILED,
        StableCode.WRONG_ACCOUNT,
        StableCode.RATE_LIMITED,
        StableCode.RISK_CONTROL,
        StableCode.UID_REQUIRED,
        StableCode.PUBLISH_OUTCOME_UNKNOWN,
        StableCode.JOB_FAILED,
        StableCode.GENSHIN_SNAPSHOT_DRIFT,
        StableCode.GENSHIN_SOURCE_CONTRACT_FAILED,
        StableCode.GENSHIN_SOURCE_RESPONSE_FAILED,
        StableCode.GENSHIN_FIELD_COVERAGE_REGRESSED,
        StableCode.GENSHIN_CANDIDATE_VALIDATION_FAILED,
        StableCode.GENSHIN_LIFECYCLE_FAILED,
        StableCode.GENSHIN_IDENTITY_CHANGED,
        StableCode.GENSHIN_ROSTER_SHRANK,
        StableCode.GENSHIN_PLAYER_LEVEL_DECREASED,
        StableCode.GENSHIN_FRIENDSHIP_DECREASED,
        StableCode.GENSHIN_CHARACTER_PROGRESSION_DECREASED,
        StableCode.GENSHIN_CHARACTER_LEVEL_DECREASED,
        StableCode.GENSHIN_ASCENSION_DECREASED,
        StableCode.GENSHIN_CONSTELLATION_DECREASED,
        StableCode.GENSHIN_NORMAL_ATTACK_DECREASED,
        StableCode.GENSHIN_ELEMENTAL_SKILL_DECREASED,
        StableCode.GENSHIN_ELEMENTAL_BURST_DECREASED,
        StableCode.GENSHIN_TIMESTAMP_REGRESSED,
        StableCode.ZZZ_FORMAL_VALIDATION_FAILED,
        StableCode.ZZZ_CANDIDATE_VALIDATION_FAILED,
        StableCode.ZZZ_CANDIDATE_CLEANUP_FAILED,
        StableCode.ZZZ_APPLY_VALIDATION_FAILED,
        StableCode.ZZZ_APPLY_FAILED,
        StableCode.ZZZ_PUBLISH_FAILED,
        StableCode.ZZZ_REMOTE_MAIN_MOVED,
        StableCode.ZZZ_MAIN_PUSH_REJECTED,
        StableCode.ZZZ_MAPPING_FAILED,
        StableCode.ZZZ_SNAPSHOT_DRIFT,
        StableCode.ZZZ_LIFECYCLE_FAILED,
        StableCode.ZZZ_SOURCE_TRANSPORT_FAILED,
        StableCode.ZZZ_SOURCE_RESPONSE_FAILED,
    }
)


def context_disposition(result: ExecutorResult) -> ContextDisposition:
    """Classify every executor result without a permissive default."""

    if result.stable_code in _REUSABLE_JOB_CODES:
        return ContextDisposition.REUSABLE
    return ContextDisposition.RESTART_REQUIRED


class _SyntheticPage:
    def __init__(self, context: _SyntheticContext) -> None:
        self._context = context
        self._closed = False
        self.url = "about:blank"

    def is_closed(self) -> bool:
        return self._closed

    async def goto(self, url: str, *, wait_until: str) -> None:
        if url != "about:blank" or wait_until != "commit":
            raise ContextNotReusable("Synthetic Owner Page attempted network access")
        self.url = url

    async def close(self) -> None:
        self._closed = True


class _SyntheticContext:
    def __init__(self) -> None:
        self.pages: list[_SyntheticPage] = []

    async def new_page(self) -> _SyntheticPage:
        page = _SyntheticPage(self)
        self.pages.append(page)
        return page


@asynccontextmanager
async def _synthetic_owner_browser(
    settings: Settings,
    *,
    repo_root: Path,
) -> AsyncIterator[OwnerBrowserSession]:
    manager = ProfileManager(settings, process_scanner=lambda _: False)
    profile_lock = manager._acquire_owner_lock(repo_root=repo_root)
    context = _SyntheticContext()
    page = await context.new_page()
    try:
        yield OwnerBrowserSession(
            playwright=cast(Playwright, object()),
            context=cast(BrowserContext, context),
            page=cast(Page, page),
            headed=True,
            network_blocked=True,
        )
    finally:
        profile_lock.release()


@asynccontextmanager
async def _owner_browser_lifecycle(
    settings: Settings,
    *,
    repo_root: Path,
    block_network: bool,
) -> AsyncIterator[OwnerBrowserSession]:
    if settings.test_mode:
        async with _synthetic_owner_browser(
            settings,
            repo_root=repo_root,
        ) as session:
            yield session
        return
    manager = ProfileManager(settings)
    async with persistent_owner_browser(
        manager,
        repo_root=repo_root,
        headed=True,
        block_network=block_network,
    ) as session:
        yield session


async def _execute_with_heartbeat(
    queue: JobQueue,
    settings: Settings,
    *,
    owner_id: str,
    runtime_digest_value: str,
    job_id: str,
    execute: asyncio.Task[ExecutorResult],
) -> ExecutorResult:
    try:
        while True:
            done, _ = await asyncio.wait(
                {execute},
                timeout=settings.worker_heartbeat_seconds,
            )
            if execute in done:
                return await execute
            queue.heartbeat(
                owner_id,
                job_id,
                runtime_digest=runtime_digest_value,
            )
    finally:
        if not execute.done():
            execute.cancel()
            await asyncio.gather(execute, return_exceptions=True)


async def run_owner(
    settings: Settings,
    *,
    repo_root: Path,
    block_network: bool = False,
    runtime_digest_override: str | None = None,
    propagate_failure: bool = False,
) -> int:
    """Keep one headed persistent Context alive until explicit shutdown."""

    queue = JobQueue(settings)
    worker_lock = FileLock(settings.worker_lock_file)
    if not worker_lock.acquire(blocking=False):
        return 0
    owner_id = str(uuid.uuid4())
    runtime_digest_value = (
        runtime_digest(settings) if runtime_digest_override is None else runtime_digest_override
    )
    request_gate = RequestGate(settings)
    normal_shutdown = False
    broken = False
    owner_started = False
    worker_released = False
    cancelled: asyncio.CancelledError | None = None
    failure: BaseException | None = None
    persistent_profile_broken = False
    retention_due = True
    try:
        queue.begin_owner(owner_id, runtime_digest=runtime_digest_value)
        owner_started = True
        queue.reconcile_uncertain_results()
        queue.sweep_protocol_files()
        queue.reconcile_stale_running(owner_id)
        maintenance = queue.run_retention_maintenance()
        queue.observe_retention_maintenance(maintenance)
        retention_due = False
        browser_lifecycle = _owner_browser_lifecycle(
            settings,
            repo_root=repo_root,
            block_network=block_network,
        )
        async with browser_lifecycle as session:
            context_generation = str(uuid.uuid4())
            queue.mark_owner_running(
                owner_id,
                context_generation=context_generation,
            )
            while True:
                if commit_shutdown_decision(
                    settings,
                    queue,
                    owner_id=owner_id,
                ):
                    normal_shutdown = True
                    break
                uncertain = queue.reconcile_uncertain_results()
                stale = queue.reconcile_stale_running(owner_id)
                if uncertain["recovered"] or stale["recovered"] or stale["abandoned"]:
                    retention_due = True
                if retention_due:
                    maintenance = queue.run_retention_maintenance()
                    queue.observe_retention_maintenance(maintenance)
                    retention_due = False
                outcome = queue.claim_next(
                    owner_id,
                    runtime_digest=runtime_digest_value,
                )
                if outcome.job is not None:
                    lease = BrowserJobLease(
                        session.context,
                        request_gate=request_gate,
                        context_generation=context_generation,
                    )
                    adapter = operation_adapter(
                        outcome.job.game,
                        outcome.job.operation,
                    )
                    if adapter is None:
                        if (
                            not settings.test_mode
                            or outcome.job.operation not in SYNTHETIC_OPERATIONS
                        ):
                            raise OwnerBroken("claimed Job operation is not registered")
                        browser_authority = BrowserAuthority.ROUTINE
                    else:
                        browser_authority = adapter.browser_authority
                    borrowed = (
                        lease.borrow_for_login(session.page)
                        if browser_authority is BrowserAuthority.LOGIN
                        else lease.borrow()
                    )
                    task = asyncio.create_task(
                        execute_registered(
                            outcome.job,
                            settings,
                            browser=borrowed,
                            request_gate=request_gate,
                        )
                    )
                    try:
                        value = await _execute_with_heartbeat(
                            queue,
                            settings,
                            owner_id=owner_id,
                            runtime_digest_value=runtime_digest_value,
                            job_id=outcome.job.job_id,
                            execute=task,
                        )
                        disposition = context_disposition(value)
                        job_cancelled: asyncio.CancelledError | None = None
                    except asyncio.CancelledError as error:
                        disposition = ContextDisposition.RESTART_REQUIRED
                        job_cancelled = error
                        value = ExecutorResult(stable_code=StableCode.OWNER_BROKEN)
                    except BaseException:
                        disposition = ContextDisposition.RESTART_REQUIRED
                        job_cancelled = None
                        value = ExecutorResult(stable_code=StableCode.OWNER_BROKEN)

                    cleanup_failed = False
                    try:
                        residue = await lease.cleanup()
                        if not residue.clean:
                            cleanup_failed = True
                    except asyncio.CancelledError as error:
                        cleanup_failed = True
                        if job_cancelled is None:
                            job_cancelled = error
                    except BaseException:
                        cleanup_failed = True
                    try:
                        await seal_context_pages(
                            session.context,
                            session.page,
                        )
                    except asyncio.CancelledError as error:
                        cleanup_failed = True
                        if job_cancelled is None:
                            job_cancelled = error
                    except BaseException:
                        cleanup_failed = True

                    if (
                        job_cancelled is not None
                        or cleanup_failed
                        or disposition is ContextDisposition.RESTART_REQUIRED
                    ):
                        broken = True
                    try:
                        queue.finish(outcome.job, owner_id, value)
                    except BaseException:
                        broken = True
                        if job_cancelled is not None:
                            raise job_cancelled from None
                        raise
                    retention_due = True
                    if job_cancelled is not None:
                        raise job_cancelled
                    queue.heartbeat(
                        owner_id,
                        runtime_digest=runtime_digest_value,
                    )
                    if broken:
                        break
                    continue
                if outcome.state_changed:
                    queue.sweep_protocol_files()
                retention_due = True
                queue.heartbeat(
                    owner_id,
                    runtime_digest=runtime_digest_value,
                )
                now = time.time()
                delay = settings.worker_heartbeat_seconds
                if outcome.next_check_at is not None:
                    delay = min(delay, max(0.01, outcome.next_check_at - now))
                await asyncio.sleep(max(0.01, delay))
    except asyncio.CancelledError as error:
        broken = True
        cancelled = error
    except ProfileBroken as error:
        broken = True
        persistent_profile_broken = True
        failure = error
    except BaseException as error:
        broken = True
        failure = error
    finally:
        if normal_shutdown and not broken:
            try:
                with locked_protocol(settings):
                    marker = read_shutdown_marker(settings)
                    if marker is None or marker.owner_id != owner_id:
                        raise ProtocolStateError("Owner shutdown marker authority was lost")
                    queued, running = queue.queue_counts()
                    if queued or running:
                        raise OwnerQueueNotEmpty("Owner cannot exit with a non-empty queue")
                    queue.require_login_after_owner_stop()
                    queue.clear_worker(owner_id)
                    worker_lock.release()
                    worker_released = True
                    remove_shutdown_marker(settings, owner_id=owner_id)
            except BaseException as error:
                broken = True
                if failure is None:
                    failure = error
        if broken:
            if owner_started:
                try:
                    queue.break_owner_and_terminalize_queued(
                        owner_id,
                        persistent_profile_broken=persistent_profile_broken,
                    )
                except BaseException as error:
                    if failure is None:
                        failure = error
                    with contextlib.suppress(BaseException):
                        queue.mark_owner_broken(owner_id)
                    with contextlib.suppress(BaseException):
                        queue.require_login_after_owner_stop()
            if not worker_released:
                worker_lock.release()
                worker_released = True
        elif not worker_released:
            worker_lock.release()
    if cancelled is not None:
        raise cancelled
    if failure is not None and propagate_failure:
        raise failure
    return 1 if broken else 0
