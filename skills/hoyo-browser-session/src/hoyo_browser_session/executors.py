"""Fixed in-process Owner Registry and isolated synthetic child protocol."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from .blocking import operation_deadline
from .browser import BrowserOwnerSession
from .config import (
    SYNTHETIC_OPERATION_CONTRACT_VERSION,
    SYNTHETIC_OPERATIONS,
    Settings,
    playwright_subprocess_environment,
)
from .errors import (
    AuthenticationRequired,
    LoginBindingFailed,
    LoginNetworkError,
    LoginTimeout,
    PublishOutcomeUnknown,
    RateLimited,
    RiskControl,
    WrongAccount,
)
from .locks import ensure_private_directory
from .models import ExecutorResult, Job, JobState, StableCode, result_json
from .operation_registry import OWNER_OPERATION_REGISTRY
from .request_gate import RequestGate
from .worker_protocol import (
    ResultFileInvalid,
    ResultFileMissing,
    executor_result_from_job_result,
    read_worker_result,
    result_paths,
)

_SYNTHETIC_CODES = {
    "synthetic-success": StableCode.PASS,
    "synthetic-failure": StableCode.JOB_FAILED,
    "synthetic-rate-limit": StableCode.RATE_LIMITED,
    "synthetic-risk-control": StableCode.RISK_CONTROL,
}


def synthetic_child_arguments(settings: Settings, job: Job) -> list[str]:
    return [
        sys.executable,
        "-m",
        "hoyo_browser_session",
        "_synthetic-execute",
        "--test-root",
        str(settings.root),
        "--job-id",
        job.job_id,
        "--game",
        job.game,
        "--operation",
        job.operation,
        "--request-interval",
        str(settings.shared_request_min_interval_seconds),
        "--execution-delay",
        str(settings.synthetic_delay_seconds),
        "--timeout-delay",
        str(max(settings.executor_timeout_seconds * 2, 0.1)),
    ]


async def _execute_child(
    job: Job,
    settings: Settings,
    arguments: list[str],
) -> ExecutorResult:
    ensure_private_directory(settings.broker_tmp)
    final, temporary = result_paths(settings, job.job_id)
    if os.path.lexists(final) or os.path.lexists(temporary):
        return ExecutorResult(stable_code=StableCode.JOB_FAILED)
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            *arguments,
            env=playwright_subprocess_environment(settings),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            close_fds=True,
        )
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=settings.executor_timeout_seconds,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
        try:
            return executor_result_from_job_result(read_worker_result(settings, job))
        except (ResultFileMissing, ResultFileInvalid):
            return ExecutorResult(stable_code=StableCode.JOB_FAILED)
    except asyncio.CancelledError:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        raise
    except OSError:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        return ExecutorResult(stable_code=StableCode.JOB_FAILED)


async def execute_registered(
    job: Job,
    settings: Settings,
    *,
    browser: BrowserOwnerSession | None = None,
    request_gate: RequestGate | None = None,
) -> ExecutorResult:
    """Execute Synthetic isolation or one fixed in-process Owner callable."""

    if settings.test_mode and job.operation in SYNTHETIC_OPERATIONS:
        return await _execute_child(
            job,
            settings,
            synthetic_child_arguments(settings, job),
        )
    adapter = OWNER_OPERATION_REGISTRY.get((job.game, job.operation))
    if settings.test_mode or adapter is None:
        return ExecutorResult(stable_code=StableCode.EXECUTOR_NOT_REGISTERED)
    if browser is None or request_gate is None:
        return ExecutorResult(stable_code=StableCode.OWNER_BROKEN)
    try:
        with operation_deadline(settings.executor_timeout_seconds):
            async with asyncio.timeout(settings.executor_timeout_seconds):
                result = await adapter.execute(job, settings, browser, request_gate)
        adapter.validate_live_result(result)
        return result
    except TimeoutError:
        return ExecutorResult(stable_code=StableCode.OWNER_BROKEN)
    except asyncio.CancelledError:
        raise
    except (
        AuthenticationRequired,
        LoginBindingFailed,
        LoginNetworkError,
        LoginTimeout,
        PublishOutcomeUnknown,
        RateLimited,
        RiskControl,
        WrongAccount,
    ) as error:
        return ExecutorResult(stable_code=StableCode(error.code))


async def run_synthetic_child(
    settings: Settings,
    *,
    job_id: str,
    game: str,
    operation: str,
    execution_delay: float,
    timeout_delay: float,
) -> int:
    if not settings.test_mode or operation not in SYNTHETIC_OPERATIONS:
        return 2
    if game not in {"genshin", "zzz", "hsr"}:
        return 2
    ensure_private_directory(settings.broker_tmp)
    final, temporary = result_paths(settings, job_id)
    if os.path.lexists(final) or os.path.lexists(temporary):
        return 2
    await RequestGate(settings).reserve(requested_min_interval=0)
    if operation == "synthetic-timeout":
        await asyncio.sleep(timeout_delay)
        return 3
    await asyncio.sleep(execution_delay)
    code = _SYNTHETIC_CODES[operation]
    status = JobState.SUCCEEDED if code is StableCode.PASS else JobState.FAILED
    job = Job(
        job_id=job_id,
        account_scope=settings.account_scope,
        game=game,
        operation=operation,
        repo_root=Path("."),
        dedupe_key=f"hoyo-game-data:{settings.account_scope}:{game}:{operation}",
        parameter_digest="0" * 64,
        force=False,
        state=JobState.RUNNING,
        created_at=0,
        not_before_at=0,
        started_at=0,
    )
    payload = result_json(
        job=job,
        status=status,
        result=ExecutorResult(
            stable_code=code,
            request_count=1,
            data_modified=False,
        ),
        completed_at=time.time(),
        operation_contract_version=SYNTHETIC_OPERATION_CONTRACT_VERSION,
    )
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                json.loads(payload),
                handle,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, final)
    except BaseException:
        if os.path.lexists(temporary):
            temporary.unlink()
        raise
    return 0
