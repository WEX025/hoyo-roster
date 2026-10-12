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
    GAMES,
    LocalSnapshotStore,
    WorkspaceError,
    load_configuration,
)

MODULES = {game: f"{game}_character_state_sync.cli" for game in GAMES}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--user", required=True)
    parser.add_argument(
        "--source-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument(
        "operation",
        choices=("init", "sync", "login", "migrate", "inspect", "cleanup-candidate"),
    )
    parser.add_argument("game", choices=tuple(GAMES))
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--run-id")
    return parser


async def run(args: argparse.Namespace) -> int:
    config = load_configuration(args.config, args.user, args.source_root)
    store = LocalSnapshotStore(args.source_root, config, args.game)
    with store.locked():
        if args.operation in {"inspect", "cleanup-candidate"}:
            candidate_module = importlib.import_module(
                f"{args.game}_character_state_sync.candidate"
            )
            if args.operation == "cleanup-candidate":
                if args.run_id is None:
                    raise WorkspaceError("CANDIDATE_RUN_ID_REQUIRED")
                candidate_module.cleanup_candidate(
                    args.run_id, candidate_base=store.candidate_base
                )
                print("LOCAL_CANDIDATE=CLEANED")
            else:
                current = store.current()
                allowed = {"current.json", "candidates"}
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
        if args.run_id is not None:
            raise WorkspaceError("UNEXPECTED_CANDIDATE_RUN_ID")
        workspace = store.prepare()
        if args.operation == "init":
            print("LOCAL_WORKSPACE=READY")
            return 0
        if args.operation == "migrate":
            store.import_legacy()
            print("LOCAL_WORKSPACE=MIGRATED")
            return 0
        # Fixed lazy adapters; no module or executable comes from user configuration.
        module = importlib.import_module(MODULES[args.game])
        arguments = [
            "sync",
            "--repo-root",
            str(workspace),
            "--browser-profile",
            str(store.browser_profile),
            "--uid",
            config.uid(args.game),
            "--headless" if args.headless else "--headed",
            "--request-interval",
            "2.0",
        ]
        if args.operation == "login":
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
    except KeyboardInterrupt:
        print("LOCAL_WORKSPACE=FAIL\nERROR_CODE=INTERRUPTED")
        return 130
    except Exception:
        print("LOCAL_WORKSPACE=FAIL\nERROR_CODE=LOCAL_OPERATION_FAILED")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
