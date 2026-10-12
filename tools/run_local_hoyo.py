#!/usr/bin/env python3
"""Collect into an external personal workspace; never fetch, commit, or push."""

from __future__ import annotations

import argparse
import asyncio
import importlib
from pathlib import Path

from hoyo_browser_session.errors import BrokerError
from hoyo_browser_session.user_workspace import (
    DEFAULT_CONFIG,
    DEFAULT_DIRECT_ROOT,
    GAMES,
    LocalSnapshotStore,
    UserConfiguration,
    WorkspaceError,
    direct_configuration,
    load_configuration,
)

MODULES = {game: f"{game}_character_state_sync.cli" for game in GAMES}
OPERATIONS = ("init", "sync", "login", "migrate", "inspect", "cleanup-candidate")


class PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse's original message can echo a UID or another sensitive argument.
        self.exit(2, "LOCAL_WORKSPACE=FAIL\nERROR_CODE=ARGUMENTS_INVALID\n")


def build_parser() -> argparse.ArgumentParser:
    parser = PrivateArgumentParser(description=__doc__)
    parser.add_argument("--game", dest="direct_game", choices=tuple(GAMES))
    parser.add_argument(
        "--uid", help="your own game UID; exact official Binding is required"
    )
    parser.add_argument("--operation", dest="direct_operation", choices=OPERATIONS)
    parser.add_argument(
        "--storage-root", type=Path, help="optional external direct-mode storage root"
    )
    parser.add_argument("--config", type=Path, help="advanced configuration mode only")
    parser.add_argument("--user", help="advanced configuration alias")
    parser.add_argument(
        "--source-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument(
        "operation",
        nargs="?",
        choices=OPERATIONS,
    )
    parser.add_argument("game", nargs="?", choices=tuple(GAMES))
    browser = parser.add_mutually_exclusive_group()
    browser.add_argument("--headless", action="store_true")
    browser.add_argument("--headed", dest="headless", action="store_false")
    parser.set_defaults(headless=False)
    parser.add_argument("--run-id")
    return parser


def select_configuration(
    args: argparse.Namespace,
) -> tuple[UserConfiguration, str, str]:
    direct = any(
        value is not None
        for value in (args.direct_game, args.uid, args.direct_operation)
    )
    if direct:
        if any(
            value is not None
            for value in (args.config, args.user, args.operation, args.game)
        ):
            raise WorkspaceError("ARGUMENT_MODE_CONFLICT")
        if args.direct_game is None:
            raise WorkspaceError("GAME_REQUIRED")
        if args.uid is None:
            raise WorkspaceError("UID_REQUIRED")
        config = direct_configuration(
            args.direct_game,
            args.uid,
            storage_root=args.storage_root or DEFAULT_DIRECT_ROOT,
        )
        return config, args.direct_game, args.direct_operation or "sync"
    if args.storage_root is not None:
        raise WorkspaceError("ARGUMENT_MODE_CONFLICT")
    if args.user is None:
        raise WorkspaceError("GAME_UID_OR_CONFIG_USER_REQUIRED")
    if args.operation is None:
        raise WorkspaceError("OPERATION_REQUIRED")
    if args.game is None:
        raise WorkspaceError("GAME_REQUIRED")
    config = load_configuration(
        args.config or DEFAULT_CONFIG, args.user, args.source_root
    )
    return config, args.game, args.operation


async def run(args: argparse.Namespace) -> int:
    config, game, operation = select_configuration(args)
    if args.run_id is not None and operation != "cleanup-candidate":
        raise WorkspaceError("UNEXPECTED_CANDIDATE_RUN_ID")
    store = LocalSnapshotStore(args.source_root, config, game)
    with store.locked():
        if operation in {"inspect", "cleanup-candidate"}:
            candidate_module = importlib.import_module(
                f"{game}_character_state_sync.candidate"
            )
            if operation == "cleanup-candidate":
                if args.run_id is None:
                    raise WorkspaceError("CANDIDATE_RUN_ID_REQUIRED")
                candidate_module.cleanup_candidate(
                    args.run_id, candidate_base=store.candidate_base
                )
                print("LOCAL_CANDIDATE=CLEANED")
            else:
                current = store.current()
                allowed = {"current.json", "candidates", "browser-profile"}
                if current:
                    allowed.add(current.name)
                print(f"CURRENT_WORKSPACE={'PRESENT' if current else 'EMPTY'}")
                print(
                    f"GENERATION_RESIDUE_COUNT={sum(p.name not in allowed for p in store.root.iterdir())}"
                )
                for item in candidate_module.inspect_candidates(
                    candidate_base=store.candidate_base
                ):
                    print(
                        f"CANDIDATE_RUN_ID={item.run_id}\nCANDIDATE_STATUS={item.status}"
                    )
            return 0
        workspace = store.prepare()
        if operation == "init":
            print("LOCAL_WORKSPACE=READY")
            return 0
        if operation == "migrate":
            store.import_legacy()
            print("LOCAL_WORKSPACE=MIGRATED")
            return 0
        # Fixed lazy adapters; no module or executable comes from user configuration.
        module = importlib.import_module(MODULES[game])
        arguments = [
            "sync",
            "--repo-root",
            str(workspace),
            "--browser-profile",
            str(store.browser_profile),
            "--uid",
            config.uid(game),
            "--headless" if args.headless else "--headed",
            "--request-interval",
            "2.0",
        ]
        if operation == "login":
            arguments.append("--login-check-only")
        domain_args = module.build_parser().parse_args(arguments)
        try:
            return await module.run_sync(domain_args, snapshot_store=store)
        except module.SyncError as error:
            # Domain error reporters never emit source values or raw exceptions.
            code, _ = module.public_error(error)
            print(f"LOCAL_WORKSPACE=FAIL\nERROR_CODE={code}")
            return 1
        except BrokerError as error:
            print(f"LOCAL_WORKSPACE=FAIL\nERROR_CODE={error.code}")
            return 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except WorkspaceError as error:
        print(f"LOCAL_WORKSPACE=FAIL\nERROR_CODE={error}")
        return 1
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("LOCAL_WORKSPACE=FAIL\nERROR_CODE=INTERRUPTED")
        return 130
    except Exception:
        print("LOCAL_WORKSPACE=FAIL\nERROR_CODE=LOCAL_OPERATION_FAILED")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
