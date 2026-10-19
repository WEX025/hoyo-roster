"""Genshin two-pass collection and saving through a live client session."""

from datetime import UTC, datetime
from functools import partial

from hoyo_roster.blocking import run_blocking
from hoyo_roster.errors import AuthenticationRequired, WrongAccount
from hoyo_roster.progress import emit
from hoyo_roster.sync import OperationResult, publish_snapshot
from .source import OfficialCollector
from .candidate import assert_no_stale_candidates, create_candidate, prevalidate_repository
from .lifecycle import evaluate_lifecycle
from .model import Snapshot
from .normalize import normalize_snapshot, select_binding, validate_complete_list
from .snapshot import assert_stable


async def _pass(collector, args, number):
    binding = select_binding(await collector.binding(), args.uid)
    if binding["server"] != args.expected_region:
        raise WrongAccount()
    emit("COLLECTING_" + str(number))
    data, size = await collector.cultivation_list(uid=args.uid, region=args.expected_region)
    avatars = validate_complete_list(data, request_size=size)
    return normalize_snapshot(binding, avatars, synced_at=datetime(2000, 1, 1, tzinfo=UTC))


async def _run_sync_once(args, *, snapshot_store, session):
    await run_blocking(assert_no_stale_candidates, candidate_base=snapshot_store.candidate_base)
    await run_blocking(prevalidate_repository, args.repo_root)
    collector = OfficialCollector(session)
    first = await _pass(collector, args, 1)
    second = await _pass(collector, args, 2)
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
    return OperationResult("genshin", "sync", collector.request_count, lifecycle.no_op, modified)


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
