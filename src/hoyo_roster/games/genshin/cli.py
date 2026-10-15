"""Safe-default CLI for Genshin partial synchronization."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from contextlib import (
    AbstractAsyncContextManager,
)
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hoyo_roster.progress import emit
from hoyo_roster.blocking import run_blocking
from hoyo_roster.config import production_settings
from hoyo_roster.login import coordinate_login
from hoyo_roster.request_gate import RequestGate
from hoyo_roster.user_workspace import SnapshotStore
from hoyo_roster.sync import OperationResult, publish_snapshot, operation_browser
from functools import partial

from .browser import (
    BrowserSession,
    OfficialCollector,
    persistent_browser,
    resolve_browser_profile,
)
from .candidate import (
    assert_no_stale_candidates,
    create_candidate,
    prevalidate_repository,
)
from .errors import (
    AuthenticationError,
    ConfigurationError,
)
from .errors import SyncError as SyncError
from .lifecycle import evaluate_lifecycle
from .login_provider import create_routine_login_provider
from .model import Snapshot
from .normalize import (
    normalize_snapshot,
    select_binding,
    validate_complete_list,
)
from .snapshot import assert_stable


def _repo_root(value: Path) -> Path:
    root = value.expanduser().absolute()
    required = (
        root / "data/genshin-impact/profile.json",
        root / "schemas/genshin-impact/build.schema.json",
        root / "tools/validate_repository.py",
    )
    if not all(path.is_file() and not path.is_symlink() for path in required):
        raise ConfigurationError("repo-root does not contain the repository contract")
    return root


async def _binding_with_login(
    collector: OfficialCollector,
    session: BrowserSession,
    *,
    repo_root: Path,
    uid: str,
    request_gate: RequestGate | None,
    request_interval: float,
    expected_region: str | None = None,
) -> dict[str, Any]:
    provider = create_routine_login_provider(
        context=session.context,
        page=session.page,
        collector=collector,
        uid=uid,
        headed=session.headed,
        request_gate=request_gate,
        request_interval=request_interval,
        expected_region=expected_region,
    )

    def print_waiting() -> None:
        print("WAITING_FOR_USER_LOGIN=YES", flush=True)

    waiting_callback: Callable[[], None] = print_waiting
    settings = production_settings()
    login_result = await coordinate_login(
        repo_root=repo_root,
        context=session.context,
        page=session.page,
        request_gate=request_gate,
        providers=(provider,),
        headed=session.headed,
        poll_interval=settings.login_probe_interval_seconds,
        timeout=settings.login_timeout_seconds,
        waiting_callback=waiting_callback,
    )
    if not login_result.available_coverage_success or provider.binding_data is None:
        raise AuthenticationError("official Binding did not confirm the expected account")
    return provider.binding_data


async def _pass(
    collector: OfficialCollector,
    session: BrowserSession,
    *,
    repo_root: Path,
    uid: str,
    request_gate: RequestGate | None,
    request_interval: float,
    expected_region: str | None = None,
    pass_number: int = 1,
) -> tuple[Snapshot, int]:
    start = collector.request_count
    binding_data = await _binding_with_login(
        collector,
        session,
        repo_root=repo_root,
        uid=uid,
        request_gate=request_gate,
        request_interval=request_interval,
        expected_region=expected_region,
    )
    binding = select_binding(binding_data, uid)
    emit(f"COLLECTING_{pass_number}")
    list_data, request_size = await collector.cultivation_list(
        uid=binding["uid"], region=binding["server"]
    )
    avatars = validate_complete_list(list_data, request_size=request_size)
    snapshot = normalize_snapshot(binding, avatars, synced_at=datetime(2000, 1, 1, tzinfo=UTC))
    return snapshot, collector.request_count - start


async def run_sync(
    args: argparse.Namespace,
    *,
    snapshot_store: SnapshotStore,
    request_gate_override: RequestGate | None = None,
) -> OperationResult:
    repo_root = await run_blocking(_repo_root, args.repo_root)
    await run_blocking(
        assert_no_stale_candidates,
        candidate_base=snapshot_store.candidate_base,
    )
    await run_blocking(prevalidate_repository, repo_root)
    request_gate = request_gate_override
    browser_context: AbstractAsyncContextManager[BrowserSession]
    browser_profile = resolve_browser_profile(repo_root, args.browser_profile)
    browser_context = persistent_browser(
        repo_root=repo_root,
        browser_profile=browser_profile,
        headed=args.headed,
    )
    collector: OfficialCollector | None = None
    async with operation_browser(browser_context) as session:
        collector = OfficialCollector(
            session.context,
            session.page,
            request_interval=args.request_interval,
            request_gate=request_gate,
        )
        if args.login_check_only:
            binding_data = await _binding_with_login(
                collector,
                session,
                repo_root=repo_root,
                uid=args.uid,
                request_gate=request_gate,
                request_interval=args.request_interval,
                expected_region=getattr(args, "expected_region", None),
            )
            select_binding(binding_data, args.uid)
            return OperationResult("genshin", "login")
        first, first_requests_after_binding = await _pass(
            collector,
            session,
            repo_root=repo_root,
            uid=args.uid,
            request_gate=request_gate,
            request_interval=args.request_interval,
            expected_region=getattr(args, "expected_region", None),
        )
        second, second_requests = await _pass(
            collector,
            session,
            repo_root=repo_root,
            uid=args.uid,
            request_gate=request_gate,
            request_interval=args.request_interval,
            expected_region=getattr(args, "expected_region", None),
            pass_number=2,
        )
        first_requests = first_requests_after_binding
        assert_stable(first, second)
        snapshot = Snapshot(
            profile={
                **second.profile,
                "sync": {
                    **second.profile["sync"],
                    "synced_at": datetime.now(UTC)
                    .replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
            },
            roster=second.roster,
            builds=second.builds,
            weapon_count=second.weapon_count,
            source_semantic_digest=second.source_semantic_digest,
        )
        emit("VALIDATING")
        lifecycle = evaluate_lifecycle(repo_root, snapshot)
        modified = await publish_snapshot(
            snapshot_store,
            no_op=lifecycle.no_op,
            create=partial(
                create_candidate,
                repo_root,
                snapshot,
                uid=args.uid,
                candidate_base=snapshot_store.candidate_base,
            ),
        )
        result = OperationResult(
            "genshin", "sync", first_requests + second_requests, lifecycle.no_op, modified
        )
    return result
