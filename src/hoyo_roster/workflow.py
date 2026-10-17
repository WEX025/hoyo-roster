"""Independent games over one host-attached, globally throttled client session."""

import asyncio
import importlib

from .auth import SessionService, ParallelRecoveryRequired
from .client import ClientSession
from .errors import OperationError, ClientBrowserRequired
from .login import login
from .user_workspace import WorkspaceError, direct_configuration
from .game_runtime import GameResult, stops_account

SUPPORTED_GAMES = ("genshin", "zzz")


async def execute_game(args, session, game):
    module = importlib.import_module("hoyo_roster.games." + game + ".entry")
    try:
        return await module.run(args, session)
    except OperationError as error:
        return GameResult(
            game,
            "FAIL",
            error_code=error.code,
            stop_account=stops_account(error, error.code),
            missing_sources=getattr(error, "missing_sources", ()),
        )
    except WorkspaceError as error:
        return GameResult(
            game, "FAIL", error_code=str(error), stop_account=stops_account(error, str(error))
        )


async def parallel_games(args, session, games):
    """One worker/lease; quiesce peers before recovery, preserving committed games."""
    session.parallel = True
    tasks = {asyncio.create_task(execute_game(args, session, game)): game for game in games}
    pending, completed = set(tasks), {}
    recovery = False
    try:
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            stop = False
            for task in done:
                try:
                    result = task.result()
                    completed[tasks[task]] = result
                    stop |= result.stop_account
                except ParallelRecoveryRequired:
                    recovery = True
            if stop or recovery:
                break
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        drain = getattr(session.client.port, "drain", None)
        if drain is not None:
            await drain()
        session.parallel = False
    if recovery and not any(r.stop_account for r in completed.values()):
        print("PARALLEL_RECOVERY=QUIESCED\nPARALLEL_MODE=SERIAL_RECOVERY", flush=True)
        try:
            await session.recover()
        except BaseException:
            for result in completed.values():
                result.report(finalized=False)
            raise
        session.parallel_recovered = True
        for game in games:
            if game not in completed:
                print(f"PAGE_RESTART={game}", flush=True)
                result = await execute_game(args, session, game)
                completed[game] = result
                if result.stop_account:
                    break
    return [completed[g] for g in games if g in completed]


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
    if getattr(args, "collection_run", None) is not None:
        from .collection_state import CollectionCheckpoint

        if not args.client_stdio or args.operation != "sync" or args.game != "all":
            raise WorkspaceError("WORKFLOW_OPTIONS_INVALID")
        checkpoint = CollectionCheckpoint(args.collection_run)
        config = checkpoint.store.read()["config"]
        if (
            args.storage_root != checkpoint.store.root / "results"
            or str(args.runtime_root) != config["runtime_root"]
        ):
            raise WorkspaceError("WORKFLOW_OPTIONS_INVALID")
        args._collection_checkpoint = checkpoint
    results = []
    try:
        async with SessionService(client, args.runtime_root) as session:
            if getattr(args, "_collection_checkpoint", None) is not None and not client.page_reader:
                raise WorkspaceError("WORKFLOW_PAGE_READER_REQUIRED")
            if args.operation == "login":
                await login(client)
                print("LOGIN_STATUS=USER_CONFIRMED", flush=True)
                return 0
            if (
                getattr(args, "parallel_games", True)
                and session.client.page_reader
                and len(games) > 1
            ):
                results.extend(await parallel_games(args, session, games))
            else:
                for game in games:
                    result = await execute_game(args, session, game)
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
