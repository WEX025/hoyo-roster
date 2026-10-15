"""Shared validated candidate publication and completed-operation reporting."""

from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from .blocking import run_blocking
from .browser import (
    operation_browser as operation_browser,
    BrowserCloseFailed as BrowserCloseFailed,
)
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
