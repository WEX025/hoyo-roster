"""HSR collection is paused; offline formats and recovery remain supported."""

from hoyo_roster.user_workspace import WorkspaceError
from .errors import SyncError as SyncError


async def run_sync(*args, **kwargs):
    raise WorkspaceError("GAME_PAUSED")
