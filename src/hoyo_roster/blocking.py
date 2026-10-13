"""Drain blocking mutations before cancellation releases their authority."""

from __future__ import annotations
import asyncio
from collections.abc import Callable
from typing import Any


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
