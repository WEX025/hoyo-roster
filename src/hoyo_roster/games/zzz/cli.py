"""Command-line entrypoint with dry-run and headed mode as safe defaults."""

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
    persistent_browser,
    prime_rpc_headers,
    resolve_browser_profile,
)
from .candidate import (
    assert_no_stale_candidates,
    create_candidate,
    run_repository_validator,
)
from .endpoints import EndpointClient
from .errors import (
    CandidateValidationError,
    ConfigurationError,
    FormalRepositoryPrevalidationError,
    SnapshotConsistencyError,
    SyncError,
)
from .lifecycle import evaluate_lifecycle
from .login_provider import create_routine_login_provider
from .normalize import normalize_collection
from .snapshot import assert_stable_business_snapshots
from .transport import BrowserTransport


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
        run_repository_validator(repo_root)
    except CandidateValidationError as error:
        raise FormalRepositoryPrevalidationError(
            "formal repository prevalidation failed", diagnostic_sha256=error.diagnostic_sha256
        ) from None


async def _binding_with_login(
    endpoints: EndpointClient,
    *,
    repo_root: Path,
    browser_session: BrowserSession,
    uid: str | None,
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
    await run_blocking(_prevalidate_formal_repository, repo_root)
    request_gate = request_gate_override
    browser_context: AbstractAsyncContextManager[BrowserSession]
    browser_profile = resolve_browser_profile(repo_root, args.browser_profile)
    browser_context = persistent_browser(
        repo_root=repo_root,
        browser_profile=browser_profile,
        headed=args.headed,
    )

    transport: BrowserTransport | None = None
    async with (
        operation_browser(browser_context) as browser_session,
    ):
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
            request_gate=request_gate,
            request_interval=args.request_interval,
        )
        if args.login_check_only:
            return OperationResult("zzz", "login")
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
        pass_2_request_count = transport.request_count - request_count_start - pass_1_request_count
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
        result = OperationResult("zzz", "sync", total_live_request_count, lifecycle.no_op, modified)
    return result
