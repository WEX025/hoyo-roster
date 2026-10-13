"""Bound host commands and keep blocking work outside the browser event loop."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, asynccontextmanager, contextmanager, suppress
from contextvars import ContextVar
from pathlib import Path
from typing import Any

_DEADLINE: ContextVar[float | None] = ContextVar("hoyo_operation_deadline", default=None)


@contextmanager
def operation_deadline(seconds: float) -> Iterator[None]:
    token = _DEADLINE.set(time.monotonic() + seconds)
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def command_timeout(max_seconds: float = 60.0) -> float:
    deadline = _DEADLINE.get()
    if deadline is None:
        return max_seconds
    return max(0.01, min(max_seconds, deadline - time.monotonic()))


def bounded_command(
    arguments: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[bytes]:
    """Terminate the command's own process group before reporting a timeout."""

    with subprocess.Popen(
        arguments,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            # Git can spawn SSH, credential helpers and hooks. Killing only Git
            # would let descendants continue a push after its guard is released.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(
                arguments, timeout, output=stdout, stderr=stderr
            ) from error
        return subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)


async def run_blocking[T](
    function: Callable[..., T],
    *args: Any,
    on_cancel: Callable[[T], None] | None = None,
    **kwargs: Any,
) -> T:
    """Drain an in-flight mutation before cancellation can release its authority."""

    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if not task.cancelled() and task.exception() is None and on_cancel is not None:
            cleanup = asyncio.create_task(asyncio.to_thread(on_cancel, task.result()))
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            cleanup.result()
        raise


@asynccontextmanager
async def blocking_context[T](manager: AbstractContextManager[T]) -> AsyncIterator[T]:
    def exit_cancelled(_value: T) -> None:
        manager.__exit__(None, None, None)

    value = await run_blocking(
        manager.__enter__,
        on_cancel=exit_cancelled,
    )
    try:
        yield value
    except BaseException as error:
        if not await run_cleanup(manager.__exit__, type(error), error, error.__traceback__):
            raise
    else:
        await run_cleanup(manager.__exit__, None, None, None)


async def run_cleanup[T](function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Give owned-resource cleanup a bounded grace period after the Job deadline."""

    with operation_deadline(60):
        return await run_blocking(function, *args, **kwargs)
