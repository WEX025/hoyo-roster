"""ZZZ two-pass collection and saving through a live client session."""

from datetime import UTC, datetime
from functools import partial

from hoyo_roster.blocking import run_blocking
from hoyo_roster.errors import AuthenticationRequired, WrongAccount
from hoyo_roster.progress import emit
from hoyo_roster.sync import OperationResult, publish_snapshot
from hoyo_roster.validation import validate_game_source
from .candidate import assert_no_stale_candidates, create_candidate
from .endpoints import EndpointClient, select_role
from .transport import ClientTransport
from .lifecycle import evaluate_lifecycle
from .normalize import normalize_collection
from .snapshot import assert_stable_business_snapshots
from .errors import SnapshotConsistencyError


async def _collect(endpoints, args, number):
    binding = await endpoints.get_binding()
    role = select_role(binding, args.uid)
    if role.server != args.expected_region:
        raise WrongAccount()
    emit("COLLECTING_" + str(number))
    return await endpoints.collect_from_binding(binding, uid=args.uid)


async def _run_sync_once(args, *, snapshot_store, session):
    await run_blocking(assert_no_stale_candidates, candidate_base=snapshot_store.candidate_base)
    await run_blocking(validate_game_source, args.repo_root, "zenless-zone-zero")
    transport = ClientTransport(session)
    endpoints = EndpointClient(transport)
    first = await _collect(endpoints, args, 1)
    second = await _collect(endpoints, args, 2)
    if first.completeness != second.completeness:
        raise SnapshotConsistencyError("Source completeness changed between passes")
    assert_stable_business_snapshots(normalize_collection(first), normalize_collection(second))
    snapshot = normalize_collection(second, synced_at=datetime.now(UTC))
    emit("VALIDATING")
    lifecycle = evaluate_lifecycle(args.repo_root, snapshot)
    modified = await publish_snapshot(
        snapshot_store,
        no_op=lifecycle.no_op,
        create=partial(
            create_candidate,
            args.repo_root,
            snapshot,
            uid=args.uid,
            candidate_base=snapshot_store.candidate_base,
        ),
    )
    return OperationResult("zzz", "sync", transport.request_count, lifecycle.no_op, modified)


async def run_sync(args, *, snapshot_store, session):
    for attempt in range(2):
        try:
            return await _run_sync_once(args, snapshot_store=snapshot_store, session=session)
        except AuthenticationRequired:
            if attempt:
                raise
            print("AUTH_RECOVERY=RESTART_CURRENT_GAME", flush=True)
            await session.recover()
    raise AssertionError("unreachable")
