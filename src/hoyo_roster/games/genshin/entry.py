"""genshin owns session checks, official identities, collection and saving."""

from hoyo_roster.errors import AuthenticationRequired, WrongAccount
from hoyo_roster.models import OwnedRole
from hoyo_roster.game_runtime import execute_roles
from .candidate import candidate_policy
from .cli import run_sync
from .errors import SyncError
from .source import OfficialCollector
from .normalize import positive_id, select_binding


def owned_roles(data):
    if not isinstance(data, dict) or not isinstance(data.get("list"), list):
        raise WrongAccount()
    roles, seen = [], set()
    for item in data["list"]:
        if not isinstance(item, dict):
            raise WrongAccount()
        if item.get("game_biz") != "hk4e_cn":
            continue
        try:
            uid = positive_id(item.get("game_uid"))
            selected = select_binding(data, uid)
            region = selected["server"]
            if region not in {"cn_gf01", "cn_qd01"} or uid in seen:
                raise ValueError
        except (ValueError, SyncError):
            raise WrongAccount() from None
        seen.add(uid)
        roles.append(OwnedRole("genshin", uid, region))
    return tuple(sorted(roles, key=lambda role: (role.uid, role.region)))


async def run(args, session):
    collector = OfficialCollector(session)

    async def check():
        return owned_roles(await collector.binding())

    try:
        roles = await check()
    except AuthenticationRequired:
        roles = await session.recover(verify_current=check)
    session.pin("genshin", roles, check)
    return await execute_roles(
        args,
        session,
        roles,
        game="genshin",
        candidate_policy=candidate_policy,
        sync=run_sync,
        sync_error=SyncError,
    )
