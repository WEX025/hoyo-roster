"""Sequential task orchestration over public game modules."""

import importlib

from .auth import AuthService
from .errors import OperationError
from .progress import OPTIONS, options_from_args
from .user_workspace import WorkspaceError, direct_configuration
from .game_runtime import ACCOUNT_STOP_CODES, GameResult, stops_account

SUPPORTED_GAMES = ("genshin", "zzz")


async def run_task(args):
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
    token = OPTIONS.set(options_from_args(args))
    results = []
    close_error = None
    closed = False
    try:
        try:
            async with AuthService(
                args.auth_root, headed=not args.headless, timeout=args.login_timeout
            ) as auth:
                for game in games:
                    module = importlib.import_module(f"hoyo_roster.games.{game}.entry")
                    try:
                        result = await module.run(args, auth)
                    except OperationError as error:
                        # Identity/protection/login-interaction failures stop dependent games.
                        result = GameResult(
                            game,
                            "FAIL",
                            error_code=error.code,
                            stop_account=stops_account(error, error.code),
                        )
                    except WorkspaceError as error:
                        result = GameResult(
                            game,
                            "FAIL",
                            error_code=str(error),
                            stop_account=str(error) in ACCOUNT_STOP_CODES,
                        )
                    results.append(result)
                    if result.stop_account:
                        break
            closed = True
        except OperationError as error:
            close_error = error
    finally:
        OPTIONS.reset(token)
        # Committed business results remain visible even when browser close fails.
        for result in results:
            result.report(finalized=closed)
    if close_error:
        raise close_error
    success = (
        bool(results)
        and not any(r.failed for r in results)
        and any(r.status == "PASS" for r in results)
    )
    print(f"TASK_STATUS={'PASS' if success else 'FAIL'}", flush=True)
    if not success and results and all(r.status == "NO_ROLES" for r in results):
        print("ERROR_CODE=NO_OWNED_ROLES\nCOLLECTION_STATUS=NOT_COLLECTED", flush=True)
    return 0 if success else 1
