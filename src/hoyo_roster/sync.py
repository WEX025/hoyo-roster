"""Shared validated candidate publication and completed-operation reporting."""

from __future__ import annotations
import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager, AbstractAsyncContextManager
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from .blocking import run_blocking
from .errors import OperationError
from .progress import emit
from .user_workspace import SnapshotStore, WorkspaceError


class Candidate(Protocol):
    @property
    def game_dir(self) -> Path: ...

    def cleanup(self) -> None: ...


@dataclass(frozen=True, slots=True)
class OperationResult:
    game: str
    operation: str
    request_count: int = 0
    no_op: bool = False
    data_modified: bool = False

    def report(self) -> None:
        print(f"GAME={self.game}\nOPERATION={self.operation}\nSTATUS=PASS\nSTABLE_CODE=PASS")
        if self.operation == "login":
            print("BINDING_STATUS=PASS")
        else:
            print(
                f"REQUEST_COUNT={self.request_count}\nNO_OP={'YES' if self.no_op else 'NO'}\nDATA_MODIFIED={'YES' if self.data_modified else 'NO'}"
            )


async def publish_snapshot(
    store: SnapshotStore, *, no_op: bool, create: Callable[[], Candidate]
) -> bool:
    if no_op:
        emit("UNCHANGED")
        return False
    candidate = await run_blocking(create, on_cancel=lambda item: item.cleanup())
    try:
        emit("SAVING")
        result = await run_blocking(store.commit, candidate.game_dir)
        if result.state != "COMMITTED":
            raise WorkspaceError("COMMIT_NOT_CONFIRMED")
        emit("SAVED")
    finally:
        # Unknown persistence retains all possible evidence. This also drains after cancellation.
        await run_blocking(store.cleanup_candidate, candidate.cleanup)
    return True


class BrowserCloseFailed(OperationError):
    code = "BROWSER_CLOSE_FAILED"


@asynccontextmanager
async def operation_browser[T](
    manager: AbstractAsyncContextManager[T], *, on_closed: Callable[[], None] | None = None
) -> AsyncIterator[T]:
    session = await manager.__aenter__()
    primary = None
    try:
        yield session
    except BaseException as error:
        primary = error
        raise
    finally:
        cancelled = None
        closing = asyncio.create_task(
            manager.__aexit__(
                type(primary) if primary else None,
                primary,
                primary.__traceback__ if primary else None,
            )
        )
        try:
            while not closing.done():
                try:
                    await asyncio.shield(closing)
                except asyncio.CancelledError as error:
                    cancelled = error
            closing.result()
        except BaseException:
            print("BROWSER_CLOSE=FAILED", flush=True)
            raise BrowserCloseFailed("Browser cleanup did not complete") from None
        print("BROWSER_CLOSE=CLOSED", flush=True)
        if on_closed is not None:
            await run_blocking(on_closed)
        if cancelled is not None:
            raise cancelled
