"""Command-line entrypoint with dry-run and headed mode as safe defaults."""

from __future__ import annotations

import argparse
import hashlib
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
    persistent_browser,
    prime_rpc_headers,
    resolve_browser_profile,
)
from .candidate import (
    Candidate,
    assert_no_stale_candidates,
    create_candidate,
    run_repository_validator,
    tree_digest,
)
from .endpoints import EndpointClient
from .errors import (
    CandidateValidationError,
    ConfigurationError,
    FormalRepositoryPrevalidationError,
    SnapshotConsistencyError,
    SyncError,
)
from .jsonio import StrictJsonError, require_real_path_chain
from .lifecycle import LifecycleDecision, evaluate_lifecycle
from .login_provider import create_routine_login_provider
from .normalize import normalize_collection
from .snapshot import assert_stable_business_snapshots
from .transport import BrowserTransport


@dataclass(slots=True)
class SyncMetrics:
    request_count: int = 0
    no_op: bool = False
    data_modified: bool = False


# Public phase labels are constants, never derived from an exception message.


def _repo_root(path: Path) -> Path:
    root = path.expanduser().absolute()
    required = (
        root / "data/zenless-zone-zero/profile.json",
        root / "schemas/zenless-zone-zero/build.schema.json",
        root / "tools/validate_repository.py",
    )
    if not all(item.is_file() for item in required):
        raise ConfigurationError("repo-root does not contain the repository contract")
    return root


def _prevalidate_formal_repository(repo_root: Path) -> None:
    try:
        tools = require_real_path_chain(
            repo_root,
            Path("tools"),
            final_kind="directory",
        )
        validator = require_real_path_chain(
            repo_root,
            Path("tools/validate_repository.py"),
            final_kind="regular_file",
        )
        require_real_path_chain(repo_root, Path("data"), final_kind="directory")
        require_real_path_chain(repo_root, Path("schemas"), final_kind="directory")
        if validator.parent != tools:
            raise StrictJsonError("formal Validator parent was invalid")
    except (OSError, StrictJsonError):
        diagnostic = hashlib.sha256(b"unsafe-formal-validator").hexdigest()
        raise FormalRepositoryPrevalidationError(
            f"formal repository prevalidation failed; diagnostic_sha256={diagnostic[:12]}",
            diagnostic_sha256=diagnostic,
        ) from None
    try:
        run_repository_validator(repo_root)
    except CandidateValidationError as error:
        validator_diagnostic = error.diagnostic_sha256
        if validator_diagnostic is None:
            validator_diagnostic = hashlib.sha256(
                type(error).__qualname__.encode("utf-8")
            ).hexdigest()
        raise FormalRepositoryPrevalidationError(
            "formal repository prevalidation failed; "
            f"diagnostic_sha256={validator_diagnostic[:12]}",
            diagnostic_sha256=validator_diagnostic,
        ) from None


async def _binding_with_login(
    endpoints: EndpointClient,
    *,
    repo_root: Path,
    browser_session: BrowserSession,
    uid: str | None,
    job_id: str | None,
    request_gate: RequestGate | None,
    request_interval: float,
) -> dict[str, Any]:
    provider = create_routine_login_provider(
        context=browser_session.context,
        page=browser_session.page,
        endpoints=endpoints,
        uid=uid,
        headed=browser_session.headed,
        request_gate=request_gate,
        request_interval=request_interval,
    )

    def print_waiting() -> None:
        print("WAITING_FOR_USER_LOGIN=YES", flush=True)

    waiting_callback: Callable[[], None] = print_waiting
    settings = production_settings()
    result = await coordinate_login(
        repo_root=repo_root,
        context=browser_session.context,
        page=browser_session.page,
        request_gate=request_gate,
        providers=(provider,),
        headed=browser_session.headed,
        poll_interval=settings.login_probe_interval_seconds,
        timeout=settings.login_timeout_seconds,
        waiting_callback=waiting_callback,
    )
    if not result.success:
        raise ConfigurationError("expected ZZZ identity was unavailable")
    assert provider.binding_data is not None
    return provider.binding_data


def _print_summary(
    *,
    total_live_request_count: int,
    data_modified: bool,
    lifecycle: LifecycleDecision,
) -> None:
    print("GAME=zzz")
    print("OPERATION=sync")
    print("STATUS=PASS")
    print("STABLE_CODE=PASS")
    print(f"REQUEST_COUNT={total_live_request_count}")
    print(f"NO_OP={'YES' if lifecycle.no_op else 'NO'}")
    print(f"DATA_MODIFIED={'YES' if data_modified else 'NO'}")
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
    await run_blocking(_prevalidate_formal_repository, repo_root)
    request_gate = request_gate_override
    browser_context: AbstractAsyncContextManager[BrowserSession]
    browser_profile = resolve_browser_profile(repo_root, args.browser_profile)
    browser_context = persistent_browser(
        repo_root=repo_root,
        browser_profile=browser_profile,
        headed=args.headed,
    )
    dry_run = snapshot_store is None
    formal_digest_before = tree_digest(repo_root / "data")

    transport: BrowserTransport | None = None
    try:
        async with (
            browser_context as browser_session,
        ):
            job_id = getattr(args, "job_id", None)
            if job_id is not None and not isinstance(job_id, str):
                raise ConfigurationError("internal job ID was invalid")
            transport = BrowserTransport(
                browser_session.context,
                request_interval=args.request_interval,
                rpc_headers=browser_session.rpc_headers,
                request_gate=request_gate,
            )
            request_count_start = transport.request_count
            endpoints = EndpointClient(transport)
            binding = await _binding_with_login(
                endpoints,
                repo_root=repo_root,
                browser_session=browser_session,
                uid=args.uid,
                job_id=job_id,
                request_gate=request_gate,
                request_interval=args.request_interval,
            )
            if args.login_check_only:
                print("GAME=zzz")
                print("OPERATION=login-check")
                print("STATUS=PASS")
                print("STABLE_CODE=PASS")
                return 0
            await prime_rpc_headers(
                browser_session,
                request_gate=request_gate,
                requested_min_interval=args.request_interval,
            )
            emit("COLLECTING_1")
            first_collection = await endpoints.collect_from_binding(
                binding,
                uid=args.uid,
            )
            pass_1_request_count = transport.request_count - request_count_start
            first_snapshot = normalize_collection(first_collection)
            emit("COLLECTING_2")
            second_binding = await endpoints.get_binding()
            second_collection = await endpoints.collect_from_binding(
                second_binding,
                uid=args.uid,
            )
            pass_2_request_count = (
                transport.request_count - request_count_start - pass_1_request_count
            )
            total_live_request_count = transport.request_count - request_count_start
            if total_live_request_count != pass_1_request_count + pass_2_request_count:
                raise SyncError("business pass request counts did not reconcile")
            second_snapshot = normalize_collection(second_collection)
            if first_collection.completeness != second_collection.completeness:
                raise SnapshotConsistencyError(
                    "source completeness contract changed between business passes"
                )
            assert_stable_business_snapshots(
                first_snapshot,
                second_snapshot,
            )
            snapshot = normalize_collection(
                second_collection,
                synced_at=datetime.now(UTC),
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
                        uid=args.uid,
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
                        await run_blocking(snapshot_store.commit, candidate.zzz_dir)

            finally:
                if candidate is not None and (
                    snapshot_store is None or snapshot_store.commit_state == "COMMITTED"
                ):
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

            if dry_run and tree_digest(repo_root / "data") != formal_digest_before:
                raise SyncError("dry-run modified formal repository data")
            modified = not dry_run and not lifecycle.no_op
            if metrics is not None:
                metrics.no_op = lifecycle.no_op
                metrics.data_modified = modified
            _print_summary(
                total_live_request_count=total_live_request_count,
                data_modified=modified,
                lifecycle=lifecycle,
            )
    finally:
        if metrics is not None and transport is not None:
            metrics.request_count = transport.request_count
    return 0
