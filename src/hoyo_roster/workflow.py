"""Sequential independent games over a host-attached client session."""

import importlib

from .auth import SessionService
from .client import ClientSession
from .errors import OperationError, ClientBrowserRequired
from .login import login
from .user_workspace import WorkspaceError, direct_configuration
from .game_runtime import GameResult, stops_account

SUPPORTED_GAMES = ("genshin", "zzz")


async def run_task(args, client=None):
    if args.operation not in {"sync", "discover", "login"}:
        raise WorkspaceError("ARGUMENTS_INVALID")
    if args.game == "hsr":
        raise WorkspaceError("GAME_PAUSED")
    games = SUPPORTED_GAMES if args.game in {None, "all"} else (args.game,)
    if args.uid is not None and args.game is None:
        raise WorkspaceError("GAME_REQUIRED")
    if args.uid is not None and len(games) != 1:
        raise WorkspaceError("ARGUMENTS_INVALID")
    if args.uid is not None:
        direct_configuration(args.game, args.uid, storage_root=args.storage_root)
    if args.run_id is not None:
        raise WorkspaceError("UNEXPECTED_CANDIDATE_RUN_ID")
    if args.generation is not None:
        raise WorkspaceError("UNEXPECTED_GENERATION")
    if client is None:
        raise ClientBrowserRequired()
    if not isinstance(client, ClientSession):
        raise ClientBrowserRequired()
    results = []
    try:
        async with SessionService(client, args.runtime_root) as session:
            if args.operation == "login":
                await login(client)
                print("LOGIN_STATUS=USER_CONFIRMED", flush=True)
                return 0
            for game in games:
                module = importlib.import_module("hoyo_roster.games." + game + ".entry")
                try:
                    result = await module.run(args, session)
                except OperationError as error:
                    result = GameResult(
                        game,
                        "FAIL",
                        error_code=error.code,
                        stop_account=stops_account(error, error.code),
                        missing_sources=getattr(error, "missing_sources", ()),
                    )
                except WorkspaceError as error:
                    result = GameResult(
                        game,
                        "FAIL",
                        error_code=str(error),
                        stop_account=stops_account(error, str(error)),
                    )
                results.append(result)
                if result.stop_account:
                    break
    except BaseException:
        for result in results:
            result.report(finalized=False)
        raise
    for result in results:
        result.report()
    success = (
        bool(results)
        and not any(r.failed for r in results)
        and any(r.status == "PASS" for r in results)
    )
    print("TASK_STATUS=" + ("PASS" if success else "FAIL"), flush=True)
    if results and all(r.status == "NO_ROLES" for r in results):
        print("ERROR_CODE=NO_OWNED_ROLES\nCOLLECTION_STATUS=NOT_COLLECTED", flush=True)
    return 0 if success else 1
