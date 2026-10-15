"""Collect private local Genshin or ZZZ state.

Honkai: Star Rail is temporarily unavailable; please do not use it.
Genshin currently excludes artifacts and full stat panels.
"""

from __future__ import annotations
import argparse
import asyncio
import importlib
import re
import uuid
from pathlib import Path
from .errors import OperationError
from .progress import emit
from .validation import CONTRACT_ROOT as CONTRACT_ROOT
from .user_workspace import (
    GAMES,
    LocalSnapshotStore,
    WorkspaceError,
    direct_configuration,
)

OPERATIONS = (
    "discover",
    "init",
    "sync",
    "login",
    "inspect",
    "recover",
    "cleanup-generation",
    "cleanup-candidate",
)


def print_current_result(store: LocalSnapshotStore) -> None:
    current = store.current()
    if current is not None:
        print(f"CURRENT_RESULT={current / 'data' / store.game}")


class PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse's message can contain UID/token values. Only expose a known option name.
        option = re.match(r"argument (--[a-z-]+):", message)
        known = {action.option_strings[0] for action in self._actions if action.option_strings}
        parameter = option.group(1) if option and option.group(1) in known else "UNKNOWN"
        hint = (
            "操作可选：" + ", ".join(OPERATIONS)
            if parameter == "--operation"
            else "检查 --help 中的参数。"
        )
        self.exit(
            2,
            f"LOCAL_WORKSPACE=FAIL\nERROR_CODE=ARGUMENTS_INVALID\nPARAMETER={parameter}\n提示={hint}\n",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = PrivateArgumentParser(
        prog="hoyo-roster",
        description=__doc__,
        epilog="在线操作须由 AI 客户端内置浏览器宿主建立双向管道；本人在官方页面登录并确认。没有客户端连接时不启动浏览器。崩铁采集暂停。",
    )
    parser.add_argument(
        "--game",
        choices=(*GAMES, "all"),
        help="选择 genshin/zzz/all；默认 all，登录后自动发现本人 UID；hsr 暂停",
    )
    parser.add_argument(
        "--uid",
        help="可选的精确身份选择；默认登录后自动发现，无需预先填写；仍须官方 Binding 核验",
    )
    parser.add_argument("--operation", choices=OPERATIONS, default="sync")
    parser.add_argument(
        "--storage-root",
        type=Path,
        help="结果根目录（绝对路径）；默认为已核验源码仓库 data/，无源码时为系统用户数据根的 results/；不改变客户端浏览器会话",
    )
    parser.add_argument("--runtime-root", type=Path, help="仓库外私有调度/锁元数据根；不保存登录态")
    parser.add_argument(
        "--client-stdio", action="store_true", help="客户端宿主专用双向管道；不要在普通终端运行"
    )
    parser.add_argument("--run-id", help="owned candidate UUID for explicit cleanup")
    parser.add_argument(
        "--generation", help="verified generation UUID for recovery or non-current cleanup"
    )
    parser.add_argument("--version", action="version", version="0.1.0rc1")
    return parser


def select_configuration(args):
    if args.game is None:
        raise WorkspaceError("GAME_REQUIRED")
    if args.game == "hsr" and args.operation in {"login", "sync"}:
        raise WorkspaceError("GAME_PAUSED")
    if args.uid is None:
        raise WorkspaceError("UID_REQUIRED")
    if args.run_id is not None and args.operation != "cleanup-candidate":
        raise WorkspaceError("UNEXPECTED_CANDIDATE_RUN_ID")
    if args.generation is not None and args.operation not in {"recover", "cleanup-generation"}:
        raise WorkspaceError("UNEXPECTED_GENERATION")
    for operation, name, missing, invalid in (
        ("cleanup-candidate", "run_id", "CANDIDATE_RUN_ID_REQUIRED", "INVALID_CANDIDATE_RUN_ID"),
        ("cleanup-generation", "generation", "GENERATION_REQUIRED", "INVALID_GENERATION"),
        ("recover", "generation", None, "INVALID_GENERATION"),
    ):
        if args.operation != operation:
            continue
        value = getattr(args, name)
        if value is None:
            if missing:
                raise WorkspaceError(missing)
            continue
        try:
            if str(uuid.UUID(value)) != value:
                raise ValueError
        except ValueError:
            raise WorkspaceError(invalid) from None
    return (
        direct_configuration(args.game, args.uid, storage_root=args.storage_root),
        args.game,
        args.operation,
    )


async def run(args, client=None) -> int:
    if args.operation in {"discover", "login", "sync"}:
        from .workflow import run_task

        return await run_task(args, client=client)
    if args.client_stdio:
        raise WorkspaceError("ARGUMENTS_INVALID")
    if args.game == "all":
        raise WorkspaceError("ARGUMENTS_INVALID")
    config, game, operation = select_configuration(args)
    candidate = importlib.import_module(f"hoyo_roster.games.{game}.candidate")
    store = LocalSnapshotStore(
        CONTRACT_ROOT, config, game, candidate_policy=candidate.candidate_policy()
    )
    result = None
    try:
        with store.locked():
            print(f"RESULT_SCOPE={store.root}\nCURRENT_POINTER={store.current_file}")
            result = await run_locked(args, config, game, operation, store)
    finally:
        # Persistence remains observable even if collection, close, cancellation or lock release fails.
        print(
            f"COMMIT_STATE={store.commit_state}\nCLEANUP_PENDING={'YES' if store.cleanup_pending else 'NO'}"
        )
        if store.cleanup_pending:
            emit("CLEANUP_PENDING")
    return result


async def run_locked(args, config, game, operation, store):
    if operation == "inspect":
        state = store.inspect()
        if state["current"] == "PRESENT":
            print_current_result(store)
        print(
            f"CURRENT_WORKSPACE={state['current']}\nRECOVERY_REQUIRED={'YES' if state['recovery_required'] else 'NO'}"
        )
        for item in state["generations"]:
            print(
                f"GENERATION={item['generation']}\nGENERATION_STATUS={item['status']}\nGENERATION_CURRENT={'YES' if item['current'] else 'NO'}"
            )
        candidate = importlib.import_module(f"hoyo_roster.games.{game}.candidate")
        try:
            region = store.candidate_region
            candidate_uid = config.uid(game)
        except WorkspaceError:
            region = None
            candidate_uid = None
            print("IDENTITY_BOUNDARY=UNPROVEN")
        for item in candidate.inspect_candidates(
            candidate_base=store.candidate_base,
            uid=candidate_uid,
            region=region,
        ):
            print(
                f"CANDIDATE_RUN_ID={item.run_id}\nCANDIDATE_STATUS={item.status}\nCANDIDATE_CLEANUP_ALLOWED={'YES' if item.cleanup_allowed else 'NO'}"
            )
            if not item.cleanup_allowed:
                print(
                    "提示=候选归属或内容未通过核验；保留并私下备份，确认来源后人工处置，不按 UUID 删除。"
                )
        return 0
    if operation == "recover":
        store.recover(args.generation)
        emit("SAVED")
        print_current_result(store)
        return 0
    if operation == "cleanup-generation":
        if args.generation is None:
            raise WorkspaceError("GENERATION_REQUIRED")
        store.cleanup_generation(args.generation)
        print("LOCAL_GENERATION=CLEANED")
        return 0
    if operation == "cleanup-candidate":
        if args.run_id is None:
            raise WorkspaceError("CANDIDATE_RUN_ID_REQUIRED")
        candidate = importlib.import_module(f"hoyo_roster.games.{game}.candidate")
        try:
            candidate.cleanup_candidate(
                args.run_id,
                candidate_base=store.candidate_base,
                uid=config.uid(game),
                region=store.candidate_region,
            )
        except candidate.CandidateCleanupError as error:
            code = str(error)
            raise WorkspaceError(
                code
                if code in {"CANDIDATE_SCOPE_MISMATCH", "CANDIDATE_OWNERSHIP_UNPROVEN"}
                else "CANDIDATE_CLEANUP_FAILED"
            ) from None
        print("LOCAL_CANDIDATE=CLEANED")
        return 0
    if operation != "init":
        raise WorkspaceError("ARGUMENTS_INVALID")
    store.prepare()
    print_current_result(store)
    if store.cleanup_pending:
        return 1
    print("LOCAL_WORKSPACE=READY")
    return 0


def main(argv: list[str] | None = None, *, client=None) -> int:
    args = build_parser().parse_args(argv)
    if args.client_stdio and args.operation not in {"sync", "discover", "login"}:
        print("LOCAL_WORKSPACE=FAIL\nERROR_CODE=ARGUMENTS_INVALID")
        return 1
    if args.client_stdio and client is None:
        from .worker import serve

        return serve(args)
    try:
        return asyncio.run(run(args, client=client))
    except WorkspaceError as error:
        hint = "执行 --operation inspect；按 local-use 文档恢复。归属不明时保留并私下备份，核实来源后人工处置，不按 UUID 删除。"
        print(f"LOCAL_WORKSPACE=FAIL\nERROR_CODE={error}\n提示={hint}")
        return 1
    except OperationError as error:
        print(
            "LOCAL_WORKSPACE=FAIL\nERROR_CODE="
            + error.code
            + "\n提示=请检查客户端内置浏览器连接及受支持的数据读取能力；详见 docs/local-use.md。"
        )
        return 1
    except (KeyboardInterrupt, asyncio.CancelledError):
        emit("CANCELLED")
        print("ERROR_CODE=INTERRUPTED")
        return 130
    except Exception:
        print(
            "LOCAL_WORKSPACE=FAIL\nERROR_CODE=LOCAL_OPERATION_FAILED\n提示=执行 --operation inspect 并按文档恢复；不要提交浏览器状态或原始响应。"
        )
        return 1
