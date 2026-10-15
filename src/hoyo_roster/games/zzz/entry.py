"""Public ZZZ module: lazy authentication, owned identities, collect and save."""

from hoyo_roster.game_runtime import execute_roles
from .candidate import candidate_policy
from .cli import run_sync
from .errors import SyncError
from .login_provider import ZzzLoginProvider


async def run(args, auth):
    async with auth.page() as page:
        target = ZzzLoginProvider(auth.context, page, auth.gate, 2.0, headed=auth.headed)
        roles = await auth.owned_roles(target)
    return await execute_roles(
        args,
        auth,
        roles,
        game="zzz",
        candidate_policy=candidate_policy,
        sync=run_sync,
        sync_error=SyncError,
    )
