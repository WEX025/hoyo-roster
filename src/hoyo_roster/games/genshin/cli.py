"""Safe-default CLI for Genshin partial synchronization."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from contextlib import (
    AbstractAsyncContextManager,
)
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hoyo_roster.progress import emit
from hoyo_roster.blocking import run_blocking
from hoyo_roster.config import production_settings
from hoyo_roster.login import coordinate_login
from hoyo_roster.request_gate import RequestGate
from hoyo_roster.user_workspace import SnapshotStore

from .browser import (
    BrowserSession,
    OfficialCollector,
    persistent_browser,
    resolve_browser_profile,
)
from .candidate import (
    Candidate,
    assert_no_stale_candidates,
    create_candidate,
    prevalidate_repository,
    tree_digest,
)
from .errors import (
    AuthenticationError,
    ConfigurationError,
    SyncError,
)
from .lifecycle import LifecycleDecision, evaluate_lifecycle
from .login_provider import create_routine_login_provider
from .model import Snapshot
from .normalize import (
    normalize_snapshot,
    select_binding,
    validate_complete_list,
)
from .snapshot import assert_stable


@dataclass(slots=True)
class SyncMetrics:
    request_count: int = 0
    no_op: bool = False
    data_modified: bool = False


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
    job_id: str | None,
    request_gate: RequestGate | None,
    request_interval: float,
) -> dict[str, Any]:
    provider = create_routine_login_provider(
        context=session.context,
        page=session.page,
        collector=collector,
        uid=uid,
        headed=session.headed,
        request_gate=request_gate,
        request_interval=request_interval,
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
    job_id: str | None,
    request_gate: RequestGate | None,
    request_interval: float,
) -> tuple[Snapshot, int]:
    start = collector.request_count
    binding_data = await _binding_with_login(
        collector,
        session,
        repo_root=repo_root,
        uid=uid,
        job_id=job_id,
        request_gate=request_gate,
        request_interval=request_interval,
    )
    binding = select_binding(binding_data, uid)
    list_data, request_size = await collector.cultivation_list(
        uid=binding["uid"], region=binding["server"]
    )
    avatars = validate_complete_list(list_data, request_size=request_size)
    snapshot = normalize_snapshot(binding, avatars, synced_at=datetime(2000, 1, 1, tzinfo=UTC))
    return snapshot, collector.request_count - start


def _summary(
    *,
    pass_1_requests: int,
    pass_2_requests: int,
    lifecycle: LifecycleDecision,
    modified: bool,
) -> None:
    print("GAME=genshin")
    print("OPERATION=sync")
    print("STATUS=PASS")
    print("STABLE_CODE=PASS")
    print(f"REQUEST_COUNT={pass_1_requests + pass_2_requests}")
    print(f"NO_OP={'YES' if lifecycle.no_op else 'NO'}")
    print(f"DATA_MODIFIED={'YES' if modified else 'NO'}")
    print("COMMIT_SHA_PRESENT=NO")


async def run_sync(
    args: argparse.Namespace,
    *,
    snapshot_store: SnapshotStore | None = None,
    metrics: SyncMetrics | None = None,
    request_gate_override: RequestGate | None = None,
) -> int:
    repo_root = await run_blocking(_repo_root, args.repo_root)
    await run_blocking(
        assert_no_stale_candidates,
        **({"candidate_base": snapshot_store.candidate_base} if snapshot_store else {}),
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
    dry_run = snapshot_store is None
    before = tree_digest(repo_root / "data")
    collector: OfficialCollector | None = None
    try:
        async with browser_context as session:
            job_id = getattr(args, "job_id", None)
            if job_id is not None and not isinstance(job_id, str):
                raise ConfigurationError("internal job ID was invalid")
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
                    job_id=job_id,
                    request_gate=request_gate,
                    request_interval=args.request_interval,
                )
                select_binding(binding_data, args.uid)
                print("GAME=genshin")
                print("OPERATION=login-check")
                print("STATUS=PASS")
                print("STABLE_CODE=PASS")
                return 0
            emit("COLLECTING_1")
            first, first_requests_after_binding = await _pass(
                collector,
                session,
                repo_root=repo_root,
                uid=args.uid,
                job_id=job_id,
                request_gate=request_gate,
                request_interval=args.request_interval,
            )
            emit("COLLECTING_2")
            second, second_requests = await _pass(
                collector,
                session,
                repo_root=repo_root,
                uid=args.uid,
                job_id=job_id,
                request_gate=request_gate,
                request_interval=args.request_interval,
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
            if lifecycle.no_op:
                emit("UNCHANGED")
            candidate: Candidate | None = None
            try:
                if not lifecycle.no_op:
                    created_candidate = await run_blocking(
                        create_candidate,
                        repo_root,
                        snapshot,
                        on_cancel=Candidate.cleanup,
                        **(
                            {"candidate_base": snapshot_store.candidate_base}
                            if snapshot_store
                            else {}
                        ),
                    )
                    candidate = created_candidate
                    if snapshot_store is not None:
                        emit("SAVING")
                        await run_blocking(snapshot_store.commit, candidate.game_dir)

            finally:
                if candidate is not None:
                    try:
                        await run_blocking(candidate.cleanup)
                    except Exception:
                        if (
                            snapshot_store is None
                            or getattr(snapshot_store, "commit_state", None) != "COMMITTED"
                        ):
                            raise
                        snapshot_store.cleanup_pending = True
                        print("CANDIDATE_CLEANUP=PENDING", flush=True)
            if dry_run and tree_digest(repo_root / "data") != before:
                raise SyncError("dry-run modified formal repository data")
            modified = not dry_run and not lifecycle.no_op
            if metrics is not None:
                metrics.no_op = lifecycle.no_op
                metrics.data_modified = modified
            _summary(
                pass_1_requests=first_requests,
                pass_2_requests=second_requests,
                lifecycle=lifecycle,
                modified=modified,
            )
    finally:
        if metrics is not None and collector is not None:
            metrics.request_count = collector.request_count
    return 0
