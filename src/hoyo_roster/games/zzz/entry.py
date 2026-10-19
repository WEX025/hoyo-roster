"""zzz owns session checks, official identities, collection and saving."""

from hoyo_roster.errors import AuthenticationRequired, WrongAccount
from hoyo_roster.models import OwnedRole
from hoyo_roster.game_runtime import execute_roles
from .candidate import candidate_policy
from .cli import run_sync
from .errors import SyncError
from .transport import ClientTransport
from .endpoints import EndpointClient, select_role
from .ids import parse_role_uid
from .endpoint_specs import BINDING, ENDPOINT_REGISTRY


def owned_roles(data):
    if not isinstance(data, dict) or not isinstance(data.get("list"), list):
        raise WrongAccount()
    roles, seen = [], set()
    for item in data["list"]:
        if not isinstance(item, dict):
            raise WrongAccount()
        if item.get("game_biz") != "nap_cn":
            continue
        try:
            uid = parse_role_uid(item.get("game_uid"))
            selected = select_role(data, uid)
            region = selected.server
            if region not in {"prod_gf_cn"} or uid in seen:
                raise ValueError
        except (ValueError, SyncError):
            raise WrongAccount() from None
        seen.add(uid)
        roles.append(OwnedRole("zzz", uid, region))
    return tuple(sorted(roles, key=lambda role: (role.uid, role.region)))


async def run(args, session):
    if session.client.page_reader:
        from hoyo_roster.errors import ClientSourceUnavailable
        from hoyo_roster.page_source import PAGE_SOURCES
        from .page import run as run_page

        try:
            await session.client.require_sources("zzz", PAGE_SOURCES)
        except ClientSourceUnavailable:
            if session.parallel or getattr(args, "_collection_checkpoint", None) is not None:
                raise
            pass
        else:
            return await run_page(args, session)
    required = (BINDING.name,) if args.operation == "discover" else tuple(ENDPOINT_REGISTRY)
    await session.client.require_sources("zzz", required)
    collector = EndpointClient(ClientTransport(session))

    async def check():
        return owned_roles(await collector.get_binding())

    try:
        roles = await check()
    except AuthenticationRequired:
        roles = await session.recover(verify_current=check)
    session.pin("zzz", roles, check)
    return await execute_roles(
        args,
        session,
        roles,
        game="zzz",
        candidate_policy=candidate_policy,
        sync=run_sync,
        sync_error=SyncError,
    )
