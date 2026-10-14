"""Collect private local Genshin or ZZZ state.

Honkai: Star Rail is temporarily unavailable; please do not use it.
Genshin currently excludes artifacts and full stat panels.
"""

from __future__ import annotations
import argparse
import asyncio
import importlib
import math
import re
import uuid
from pathlib import Path
from .config import Settings
from .errors import OperationError, LoginTimeout, BrowserUnavailable, BrowserProfileInvalid
from .progress import OPTIONS, options_from_args, emit
from .request_gate import RequestGate
from .sync import OperationResult
from .user_workspace import (
    GAMES,
    LocalSnapshotStore,
    WorkspaceError,
    direct_configuration,
)

MODULES = {game: f"hoyo_roster.games.{game}.cli" for game in GAMES}
OPERATIONS = (
    "discover",
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
        epilog=(
            "所有系统默认先登录，再自动发现本人 UID，无需预先填写。省略 --game 时处理原神和绝区零。"
            "崩坏：星穹铁道暂时无法获取，请不要使用；hsr 参数仅保留接口。"
            "原神目前不含圣遗物和完整属性面板；partial 表示字段覆盖有限，角色名单仍须闭合。"
            "绝区零已支持能力保留，详见 docs/data-format.md。"
            "图片 CAPTCHA 请本人在本工具打开的官方浏览器窗口点击；AI 不自动选择、解图或绕过。"
            "验证期间暂停自动身份探测并保留会话，完成后继续本人发码/登录和精确身份核验；"
            "不自动重发短信，页面倒计时不代表实际送达。Ctrl-C 取消，窗口关闭停止。"
        ),
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
        help="结果根目录（绝对路径）；默认为已核验源码仓库 data/，无源码时为系统用户数据根的 results/；不改变认证目录",
    )
    parser.add_argument(
        "--auth-root",
        type=Path,
        help="仓库外的私有认证根目录（绝对路径）；默认系统用户数据根，须通过 POSIX 权限或 Windows SID/DACL 检查",
    )
    parser.add_argument("--login-method", choices=("qr", "phone", "cancel"))
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument(
        "--phone-stdin",
        action="store_true",
        help="read an ephemeral phone number from stdin (hidden on a terminal); requires phone and --allow-sms; never a command-line number",
    )
    parser.add_argument(
        "--allow-sms",
        action="store_true",
        help="authorize one automatic SMS click per command after the user checks official terms; requires --phone-stdin",
    )
    parser.add_argument(
        "--allow-login",
        action="store_true",
        help="authorize manual official login and local session reuse; new terms still need user review",
    )
    parser.add_argument(
        "--login-timeout",
        type=float,
        default=900,
        help="total human login budget in seconds, including panel and manual CAPTCHA (default: 900)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="reuse an existing exact session; no interactive login",
    )
    parser.add_argument("--run-id", help="owned candidate UUID for explicit cleanup")
    parser.add_argument(
        "--resume-discovery",
        help="reuse an explicitly selected owned, confirmed-closed discovery UUID; requires one game",
    )
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
        direct_configuration(
            args.game, args.uid, storage_root=args.storage_root, auth_root=args.auth_root
        ),
        args.game,
        args.operation,
    )


async def run(args) -> int:
    if getattr(args, "resume_discovery", None) is not None and (
        args.uid is not None
        or args.game not in {"genshin", "zzz"}
        or args.operation not in {"discover", "login", "sync"}
    ):
        raise WorkspaceError("INVALID_DISCOVERY_RESUME")
    if getattr(args, "resume_discovery", None) is not None and (
        getattr(args, "phone_stdin", False) or getattr(args, "allow_sms", False)
    ):
        raise WorkspaceError("DISCOVERY_RESUME_SMS_DISABLED")
    phone_stdin, allow_sms = getattr(args, "phone_stdin", False), getattr(args, "allow_sms", False)
    if phone_stdin != allow_sms or (
        phone_stdin
        and (
            args.login_method != "phone"
            or args.headless
            or args.operation not in {"discover", "login", "sync"}
        )
    ):
        raise WorkspaceError("INVALID_PHONE_LOGIN_OPTIONS")
    if args.operation == "discover" or (args.uid is None and args.operation in {"login", "sync"}):
        from .discovery import run_discovery

        return await run_discovery(args)
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
    if isinstance(result, OperationResult):
        result.report()
        return 0
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
    if operation == "logout":
        store.logout()
        print("LOCAL_SESSION=REMOVED")
        return 0
    workspace = store.prepare()
    print_current_result(store)
    if store.cleanup_pending:
        return 0 if operation == "init" else 1
    if operation == "init":
        print("LOCAL_WORKSPACE=READY")
        return 0
    module = importlib.import_module(MODULES[game])
    gate = RequestGate(Settings(external_root=store.auth_root))
    domain_args = argparse.Namespace(
        repo_root=workspace,
        browser_profile=store.browser_profile,
        uid=config.uid(game),
        expected_region=getattr(args, "discovered_region", None),
        headed=not args.headless,
        request_interval=2.0,
        login_check_only=operation == "login",
        login_wait_seconds=args.login_timeout,
    )
    token = OPTIONS.set(options_from_args(args))
    try:
        result = await module.run_sync(
            domain_args, snapshot_store=store, request_gate_override=gate
        )
        print_current_result(store)
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
        hint = "执行 --operation inspect；按 local-use 文档恢复。归属不明时保留并私下备份，核实来源后人工处置，不按 UUID 删除。"
        if str(error) == "DISCOVERY_PROFILE_CONFLICT":
            hint = "新发现会话与已有身份会话均保留，未覆盖或继续登录；先核验已有身份会话，参阅 local-use 的发现恢复步骤。"
        elif str(error) in {"DISCOVERY_RESUME_UNPROVEN", "DISCOVERY_RESUME_SMS_DISABLED"}:
            hint = "保留发现范围；只能显式复用已获证且确认关闭的会话，不能沿用自动发码或忽略未知关闭状态，参阅 local-use。"
        print(f"LOCAL_WORKSPACE=FAIL\nERROR_CODE={error}\n提示={hint}")
        return 1
    except OperationError as error:
        if isinstance(error, LoginTimeout):
            emit("TIMEOUT")
        if isinstance(error, BrowserUnavailable):
            hint = "恢复会话选定的浏览器；Chromium 缺失时，在同一虚拟环境执行 python -m playwright install chromium。"
        elif isinstance(error, BrowserProfileInvalid):
            hint = "保留原会话目录；旧布局不迁移。新会话需显式使用新的仓库外 --auth-root，并由本人首次登录。"
        elif error.code == "REQUEST_SCHEDULE_INVALID":
            hint = "持久节流时间超过等待预算；检查系统时钟并保留 request-state.json，不清零或删除来绕过节流。"
        else:
            hint = "检查官方登录页面；需登录时选择 --login-method qr 或 phone 并确认登录前说明（非交互需 --allow-login）；操控失败可重新运行，Ctrl-C 取消。"
        print(f"LOCAL_WORKSPACE=FAIL\nERROR_CODE={error.code}\n提示={hint}")
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
