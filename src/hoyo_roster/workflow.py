"""Independent games over one host-attached, globally throttled client session."""

import asyncio
import importlib

from .auth import SessionService, ParallelRecoveryRequired
from .client import ClientSession
from .errors import OperationError, ClientBrowserRequired
from .user_workspace import WorkspaceError, direct_configuration
from .game_runtime import GameResult, stops_account
from .progress import GameProgress

SUPPORTED_GAMES = ("genshin", "zzz")


async def execute_game(args, session, progress):
    game = progress.game
    progress.update("CHECKING_IDENTITY")
    module = importlib.import_module("hoyo_roster.games." + game + ".entry")
    try:
        result = await module.run(args, session, progress)
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
            game, "FAIL", error_code=str(error), stop_account=stops_account(error, str(error))
        )
    if result.status == "PASS":
        state = "VERIFIED" if args.operation == "discover" else "COMPLETED"
    else:
        state = "FAILED" if result.failed else "NO_ROLES"
    progress.update(state)
    return result


async def parallel_attempt(args, session, remaining, completed):
    session.parallel = True
    tasks = {
        asyncio.create_task(execute_game(args, session, progress)): progress
        for progress in remaining
    }
    pending = set(tasks)
    recovery = None
    try:
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                try:
                    completed[tasks[task].game] = task.result()
                except ParallelRecoveryRequired as error:
                    recovery = error
            if any(r.stop_account for r in completed.values()) or recovery is not None:
                break
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        try:
            drain = getattr(session.client.port, "drain", None)
            if drain is not None:
                await drain()
        finally:
            session.parallel = False
    if recovery is not None and not any(r.stop_account for r in completed.values()):
        startup = all(progress.state != "COLLECTING" for progress in remaining)
        for progress in remaining:
            if progress.game not in completed:
                progress.update("WAITING_LOGIN")
        return recovery.version, startup
    return None


async def parallel_games(args, session, games):
    print("COLLECTION_MODE=PARALLEL", flush=True)
    completed = {}
    try:
        while True:
            remaining = [progress for game, progress in games.items() if game not in completed]
            recovery = await parallel_attempt(args, session, remaining, completed)
            if recovery is None:
                break
            print("PARALLEL_RECOVERY=QUIESCED", flush=True)
            version, startup = recovery
            await session.resume_after_drain(version, startup=startup)
            for progress in remaining:
                if progress.game not in completed:
                    print(f"PAGE_RESTART={progress.game}", flush=True)
    except BaseException:
        for result in completed.values():
            result.report(finalized=False)
        raise
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
    progress = {game: GameProgress(game) for game in games}
    results = []
    try:
        async with SessionService(client, args.runtime_root) as session:
            if getattr(args, "_collection_checkpoint", None) is not None and not client.page_reader:
                raise WorkspaceError("WORKFLOW_PAGE_READER_REQUIRED")
            if args.operation == "login":
                await session.recover()
                print("LOGIN_STATUS=USER_CONFIRMED", flush=True)
                return 0
            if (
                getattr(args, "parallel_games", True)
                and session.client.page_reader
                and len(games) > 1
            ):
                results.extend(await parallel_games(args, session, progress))
            else:
                print("COLLECTION_MODE=SERIAL", flush=True)
                for game in games:
                    result = await execute_game(args, session, progress[game])
                    results.append(result)
                    if result.stop_account:
                        break
    except BaseException:
        for result in results:
            result.report(finalized=False)
        raise
    finally:
        if args.operation != "login":
            for state in progress.values():
                if state.state in GameProgress.unfinished:
                    state.update("CANCELLED")
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
