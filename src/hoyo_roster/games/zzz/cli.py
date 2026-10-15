"""ZZZ collection and saving through the shared account service."""

from __future__ import annotations

import argparse
from contextlib import (
    AbstractAsyncContextManager,
)
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hoyo_roster.validation import validate_game_source, ValidationFailure
from hoyo_roster.progress import emit
from hoyo_roster.blocking import run_blocking
from hoyo_roster.request_gate import RequestGate
from hoyo_roster.user_workspace import SnapshotStore
from hoyo_roster.sync import OperationResult, publish_snapshot
from functools import partial

from .browser import (
    BrowserSession,
    shared_browser,
    prime_rpc_headers,
)
from .candidate import (
    assert_no_stale_candidates,
    create_candidate,
)
from .endpoints import EndpointClient
from .errors import (
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
        validate_game_source(repo_root, "zenless-zone-zero")
    except ValidationFailure as error:
        raise FormalRepositoryPrevalidationError(
            "formal repository prevalidation failed", diagnostic_sha256=error.diagnostic
        ) from None


async def _binding_with_login(
    endpoints: EndpointClient,
    *,
    browser_session: BrowserSession,
    uid: str | None,
    request_gate: RequestGate | None,
    request_interval: float,
    auth_service,
    allow_login=True,
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

    result = await auth_service.ensure_session(provider, allow_login=allow_login)
    if not result.success:
        raise ConfigurationError("expected ZZZ identity was unavailable")
    assert provider.binding_data is not None
    return provider.binding_data


async def _run_sync_once(
    args: argparse.Namespace,
    *,
    snapshot_store: SnapshotStore,
    request_gate_override: RequestGate | None = None,
    auth_service,
) -> OperationResult:
    repo_root = await run_blocking(_repo_root, args.repo_root)
    await run_blocking(
        assert_no_stale_candidates,
        candidate_base=snapshot_store.candidate_base,
    )
    await run_blocking(_prevalidate_formal_repository, repo_root)
    request_gate = request_gate_override
    browser_context: AbstractAsyncContextManager[BrowserSession]
    browser_context = shared_browser(auth_service)
    manager = browser_context

    transport: BrowserTransport | None = None
    async with (
        manager as browser_session,
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
            browser_session=browser_session,
            uid=args.uid,
            request_gate=request_gate,
            request_interval=args.request_interval,
            auth_service=auth_service,
        )
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
        second_binding = await _binding_with_login(
            endpoints,
            browser_session=browser_session,
            uid=args.uid,
            request_gate=request_gate,
            request_interval=args.request_interval,
            auth_service=auth_service,
            allow_login=False,
        )
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


async def run_sync(args, *, snapshot_store, auth_service, request_gate_override=None):
    """Retry the entire uncommitted two-pass capture once after explicit auth loss."""
    from hoyo_roster.errors import AuthenticationRequired
    from .errors import AuthenticationError

    for attempt in range(2):
        try:
            return await _run_sync_once(
                args,
                snapshot_store=snapshot_store,
                request_gate_override=request_gate_override,
                auth_service=auth_service,
            )
        except (AuthenticationRequired, AuthenticationError):
            if attempt:
                raise AuthenticationRequired("Authentication recovery exhausted") from None
            print("AUTH_RECOVERY=RESTART_CURRENT_GAME", flush=True)
    raise AssertionError("unreachable")
