"""Collect one explicitly selected game identity into private local storage."""

from __future__ import annotations
import argparse
import asyncio
import importlib
import math
import re
import uuid
from pathlib import Path
from .config import Settings
from .errors import OperationError, LoginTimeout
from .progress import OPTIONS, LoginOptions, emit
from .request_gate import RequestGate
from .user_workspace import (
    DEFAULT_DIRECT_ROOT,
    GAMES,
    LocalSnapshotStore,
    WorkspaceError,
    direct_configuration,
)

MODULES = {game: f"hoyo_roster.games.{game}.cli" for game in GAMES}
OPERATIONS = (
    "init",
    "sync",
    "login",
    "inspect",
    "recover",
    "cleanup-generation",
    "cleanup-candidate",
    "logout",
)
CONTRACT_ROOT = Path(__file__).resolve().parent / "contracts"


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
        epilog=(
            "图片 CAPTCHA 请本人在本工具打开的官方浏览器窗口点击；AI 不自动选择、解图或绕过。"
            "验证期间暂停自动身份探测并保留会话，完成后继续本人发码/登录和精确身份核验；"
            "不自动重发短信，页面倒计时不代表实际送达。Ctrl-C 取消，窗口关闭停止。"
        ),
    )
    parser.add_argument("--game", choices=tuple(GAMES))
    parser.add_argument("--uid", help="your own UID; exact official Binding is required")
    parser.add_argument("--operation", choices=OPERATIONS, default="sync")
    parser.add_argument("--storage-root", type=Path, default=DEFAULT_DIRECT_ROOT)
    parser.add_argument("--login-method", choices=("qr", "phone", "cancel"))
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument(
        "--allow-login",
        action="store_true",
        help="authorize manual official login and local session reuse; new terms still need user review",
    )
    parser.add_argument(
        "--login-timeout",
        type=float,
        default=900,
        help="each login wait phase limit in seconds, including manual CAPTCHA (default: 900)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="reuse an existing exact session; no interactive login",
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
    if args.uid is None:
        raise WorkspaceError("UID_REQUIRED")
    if not math.isfinite(args.login_timeout) or not 0 < args.login_timeout <= 3600:
        raise WorkspaceError("INVALID_LOGIN_TIMEOUT")
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


async def run(args) -> int:
    config, game, operation = select_configuration(args)
    store = LocalSnapshotStore(CONTRACT_ROOT, config, game)
    with store.locked():
        if operation == "inspect":
            state = store.inspect()
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
            try:
                store.recover(args.generation)
            except Exception:
                print(f"COMMIT_STATE={store.commit_state}")
                raise
            emit("CLEANUP_PENDING" if store.cleanup_pending else "SAVED")
            print(f"COMMIT_STATE={store.commit_state}")
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
        if operation == "logout":
            store.logout()
            print("LOCAL_SESSION=REMOVED")
            return 0
        workspace = store.prepare()
        if store.cleanup_pending:
            emit("CLEANUP_PENDING")
            return 0 if operation == "init" else 1
        if operation == "init":
            print("LOCAL_WORKSPACE=READY")
            return 0
        module = importlib.import_module(MODULES[game])
        gate = RequestGate(Settings(external_root=store.root))
        domain_args = argparse.Namespace(
            repo_root=workspace,
            browser_profile=store.browser_profile,
            uid=config.uid(game),
            headed=not args.headless,
            request_interval=2.0,
            login_check_only=operation == "login",
            browser_channel="chromium",
            login_wait_seconds=args.login_timeout,
        )
        token = OPTIONS.set(
            LoginOptions(
                args.login_method, args.non_interactive, args.login_timeout, args.allow_login
            )
        )
        try:
            result = await module.run_sync(
                domain_args, snapshot_store=store, request_gate_override=gate
            )
            if operation == "login" and result == 0:
                # Domain browser context has now closed and flushed its local session.
                print("STATUS=PASS\nSTABLE_CODE=PASS")
            if store.cleanup_pending:
                emit("CLEANUP_PENDING")
            print(f"COMMIT_STATE={store.commit_state}")
            return result
        except module.SyncError as error:
            reporter = importlib.import_module(f"hoyo_roster.games.{game}.redaction")
            code, _ = reporter.public_error(error)
            print(
                f"LOCAL_WORKSPACE=FAIL\nERROR_CODE={code}\n提示=参阅 docs/local-use.md；身份/风险/限流错误应停止。"
            )
            return 1
        finally:
            OPTIONS.reset(token)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except WorkspaceError as error:
        print(
            f"LOCAL_WORKSPACE=FAIL\nERROR_CODE={error}\n提示=执行 --operation inspect；按 local-use 文档恢复。归属不明时保留并私下备份，核实来源后人工处置，不按 UUID 删除。"
        )
        return 1
    except OperationError as error:
        if isinstance(error, LoginTimeout):
            emit("TIMEOUT")
        print(
            f"LOCAL_WORKSPACE=FAIL\nERROR_CODE={error.code}\n提示=检查官方登录页面；需登录时选择 --login-method qr 或 phone 并确认登录前说明（非交互需 --allow-login）；操控失败可重新运行，Ctrl-C 取消。"
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
