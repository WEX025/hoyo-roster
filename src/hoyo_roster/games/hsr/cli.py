"""Safe-default CLI for HSR partial synchronization."""

from __future__ import annotations

import argparse
from collections.abc import Awaitable, Callable
from contextlib import (
    AbstractAsyncContextManager,
)
from dataclasses import dataclass, replace
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
    AvatarListDiagnostics,
    BrowserSession,
    OfficialCollector,
    SessionInitializationState,
    initialize_official_session,
    install_business_guard,
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
    BrowserLaunchFailedOther,
    BrowserProfileInUse,
    BrowserProfileStaleSingleton,
    ConfigurationError,
    CultivationOwnedRosterEmpty,
    CultivationSessionMismatch,
    ExpectedUidAmbiguous,
    ExpectedUidNotFound,
    RosterCompletenessError,
    SessionInitializationError,
    SnapshotContentDrift,
    SyncError,
)
from .evidence import SyncEvidence
from .lifecycle import (
    LifecycleDecision,
    evaluate_lifecycle,
    load_formal,
    materialize_snapshot,
)
from .login_provider import create_routine_login_provider
from .model import (
    AvatarListObservation,
    BuildCoverageStatus,
    PassObservation,
    RosterCoverageStatus,
)
from .normalize import (
    normalize_snapshot,
    select_binding,
    validate_owned_roster,
)
from .redaction import digest_prefix
from .snapshot import assert_stable


@dataclass(slots=True)
class SyncMetrics:
    request_count: int = 0
    no_op: bool = False
    data_modified: bool = False


def _repo_root(value: Path) -> Path:
    root = value.expanduser().absolute()
    required = (
        root / "data/honkai-star-rail/profile.json",
        root / "schemas/honkai-star-rail/build.schema.json",
        root / "tools/validate_repository.py",
    )
    if not all(path.is_file() and not path.is_symlink() for path in required):
        raise ConfigurationError("repo-root does not contain the repository contract")
    return root.resolve()


async def _binding_with_shared_login(
    collector: OfficialCollector,
    session: BrowserSession,
    *,
    repo_root: Path,
    uid: str,
    job_id: str | None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    provider = create_routine_login_provider(
        collector=collector,
        session=session,
        uid=uid,
    )
    settings = production_settings()

    def print_waiting() -> None:
        print("WAITING_FOR_USER_LOGIN=YES", flush=True)
        session.login_waiting_output_count += 1

    result = await coordinate_login(
        repo_root=repo_root,
        context=session.context,
        page=session.page,
        request_gate=session.request_gate,
        providers=(provider,),
        headed=session.headed,
        poll_interval=settings.login_probe_interval_seconds,
        timeout=(settings.login_timeout_seconds if timeout_seconds is None else timeout_seconds),
        waiting_callback=print_waiting,
    )
    if not result.complete_coverage_success:
        raise ConfigurationError("exact HSR Binding coverage was incomplete")
    assert provider.binding is not None
    return provider.binding


async def _binding_with_login(
    collector: OfficialCollector,
    session: BrowserSession,
    *,
    repo_root: Path,
    uid: str,
    login_wait_seconds: float = 900,
) -> dict[str, Any]:
    return await _binding_with_shared_login(
        collector,
        session,
        repo_root=repo_root,
        uid=uid,
        job_id=None,
        timeout_seconds=login_wait_seconds,
    )


def _owned_ids(avatars: list[dict[str, Any]]) -> frozenset[str]:
    identifiers: set[str] = set()
    for avatar in avatars:
        identifier = avatar.get("item_id")
        if type(identifier) is not int or identifier <= 0:
            raise RosterCompletenessError("owned character ID was invalid")
        normalized = str(identifier)
        if normalized in identifiers:
            raise RosterCompletenessError("owned character IDs were not unique")
        identifiers.add(normalized)
    if not identifiers:
        raise RosterCompletenessError("owned roster was empty")
    return frozenset(identifiers)


def _avatar_list_observation(
    diagnostics: AvatarListDiagnostics,
) -> AvatarListObservation:
    return AvatarListObservation(
        query_field_names=diagnostics.query_field_names,
        request_header_names=diagnostics.request_header_names,
        response_root_keyset_sha256=diagnostics.response_root_keyset_sha256,
        response_data_keyset_sha256=diagnostics.response_data_keyset_sha256,
        entry_keyset_sha256=diagnostics.entry_keyset_sha256,
        entry_count=diagnostics.entry_count,
        is_own_true_count=diagnostics.is_own_true_count,
        is_own_false_count=diagnostics.is_own_false_count,
        pagination_signal_count=diagnostics.pagination_signal_count,
    )


async def _pass(
    collector: OfficialCollector,
    session: BrowserSession,
    *,
    uid: str,
) -> PassObservation:
    if not session.session_initialized:
        raise SessionInitializationError("business Pass started before session initialization")
    start = collector.request_count
    binding = select_binding(await collector.binding(), uid)
    session.bind_identity(uid=binding["uid"], region=binding["server"])
    collector.bind_role(uid=binding["uid"], region=binding["server"])
    catalog = await collector.avatar_list()
    diagnostics = collector.avatar_list_diagnostics()
    try:
        avatars = validate_owned_roster(catalog)
    except CultivationOwnedRosterEmpty as error:
        avatar_list = _avatar_list_observation(diagnostics)
        if (
            error.entry_count != avatar_list.entry_count
            or error.true_count != avatar_list.is_own_true_count
            or error.false_count != avatar_list.is_own_false_count
        ):
            raise SessionInitializationError(
                "zero-owned avatar-list diagnostics disagreed"
            ) from error
        return PassObservation(
            binding=binding,
            roster_coverage=RosterCoverageStatus.UNAVAILABLE_ZERO_OWNED_OVERLAY,
            build_coverage=BuildCoverageStatus.UNAVAILABLE_NO_ROSTER,
            avatar_list=avatar_list,
            request_count=collector.request_count - start,
        )
    avatar_list = _avatar_list_observation(diagnostics)
    _owned_ids(avatars)
    details: dict[str, dict[str, Any]] = {}
    recommendations: dict[str, dict[str, Any]] = {}
    for avatar in avatars:
        character_id = str(avatar["item_id"])
        details[character_id] = await collector.avatar_detail(character_id)
        recommend = await collector.avatar_recommend(character_id)
        if recommend is not None:
            recommendations[character_id] = recommend
    snapshot = normalize_snapshot(
        binding,
        avatars,
        details,
        recommendations,
        synced_at=datetime(2000, 1, 1, tzinfo=UTC),
    )
    observation = PassObservation(
        binding=binding,
        roster_coverage=RosterCoverageStatus.OBSERVED_NONEMPTY,
        build_coverage=BuildCoverageStatus.OBSERVED,
        avatar_list=avatar_list,
        request_count=collector.request_count - start,
        snapshot=snapshot,
    )
    snapshot = replace(
        snapshot,
        source_coverage={
            **snapshot.source_coverage,
            **observation.coverage_document(),
        },
    )
    return replace(observation, snapshot=snapshot)


@dataclass(frozen=True, slots=True, repr=False)
class BootstrapEvidence:
    exact_binding: bool
    cultivation_page_opened: bool
    session_transport_ready: bool
    required_device_headers_captured: bool
    post_bootstrap_binding_match: bool
    page_sealed: bool


def _assert_two_pass_source_observed_roster_stable(
    first: object,
    second: object,
) -> None:
    if first != second:
        raise SnapshotContentDrift(
            "source-observed roster evidence drifted between Business Passes"
        )


def _assert_two_pass_observations_stable(
    first: PassObservation,
    second: PassObservation,
) -> None:
    if first.binding.get("uid") != second.binding.get("uid") or first.binding.get(
        "server"
    ) != second.binding.get("server"):
        raise SnapshotContentDrift("Business Pass identity drifted")
    if first.roster_coverage is not second.roster_coverage:
        raise SnapshotContentDrift("Business Pass roster coverage drifted")
    if first.build_coverage is not second.build_coverage:
        raise SnapshotContentDrift("Business Pass build coverage drifted")
    if first.avatar_list != second.avatar_list:
        raise SnapshotContentDrift("Business Pass avatar-list diagnostics drifted")


async def _initialize_session(
    collector: OfficialCollector,
    session: BrowserSession,
    *,
    uid: str,
    login_wait_seconds: float,
    binding_provider: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    repo_root: Path | None = None,
) -> BootstrapEvidence:
    if binding_provider is None:
        pre_binding = await _binding_with_login(
            collector,
            session,
            repo_root=Path.cwd() if repo_root is None else repo_root,
            uid=uid,
            login_wait_seconds=login_wait_seconds,
        )
    else:
        pre_binding = await binding_provider()
    collector.bind_role(
        uid=pre_binding["uid"],
        region=pre_binding["server"],
    )
    session.bind_expected_identity(
        uid=pre_binding["uid"],
        region=pre_binding["server"],
    )
    if not session.business_guard_installed:
        await install_business_guard(session)
    try:
        await initialize_official_session(session)
    except AuthenticationError:
        raise CultivationSessionMismatch(
            "cultivation session category: authentication mismatch"
        ) from None
    try:
        post_binding = select_binding(await collector.binding(), uid)
    except (AuthenticationError, ExpectedUidAmbiguous, ExpectedUidNotFound):
        raise CultivationSessionMismatch(
            "cultivation session category: post-bootstrap binding failed"
        ) from None
    if post_binding["uid"] != pre_binding["uid"] or post_binding["server"] != pre_binding["server"]:
        raise CultivationSessionMismatch("cultivation session category: binding identity changed")
    session.bind_identity(
        uid=post_binding["uid"],
        region=post_binding["server"],
    )
    collector.install_session(session)
    session.mark_initialized()
    return BootstrapEvidence(
        exact_binding=True,
        cultivation_page_opened=session.cultivation_page_opened,
        session_transport_ready=session.session_transport_ready,
        required_device_headers_captured=True,
        post_bootstrap_binding_match=True,
        page_sealed=session.bootstrap_page_sealed,
    )


def _summary(
    *,
    pass_1_requests: int,
    pass_2_requests: int,
    lifecycle: LifecycleDecision,
    modified: bool,
) -> None:
    print("GAME=hsr")
    print("OPERATION=sync")
    print("STATUS=PASS")
    print("STABLE_CODE=PASS")
    print(f"REQUEST_COUNT={pass_1_requests + pass_2_requests}")
    print(f"NO_OP={'YES' if lifecycle.no_op else 'NO'}")
    print(f"DATA_MODIFIED={'YES' if modified else 'NO'}")
    print("COMMIT_SHA_PRESENT=NO")


def _assert_login_cleanup(session: BrowserSession) -> None:
    if session.login_active:
        raise SessionInitializationError("login remained active after cleanup")
    if session.login_page_open_count and not session.login_page_sealed:
        raise SessionInitializationError("login page was not sealed")


def _bootstrap_summary(
    evidence: BootstrapEvidence,
    session: BrowserSession,
) -> None:
    if session.device_retcode != 0 or session.device_data_code != 200:
        raise SessionInitializationError("device response evidence was invalid")
    if not session.session_transport_ready or not session.bootstrap_page_sealed:
        raise SessionInitializationError("session transport bootstrap was incomplete")
    if session.initialization_state is not SessionInitializationState.PAGE_SEALED:
        raise SessionInitializationError("session initialization state was not sealed")
    if not all(
        (
            evidence.exact_binding,
            evidence.cultivation_page_opened,
            evidence.session_transport_ready,
            evidence.required_device_headers_captured,
            evidence.post_bootstrap_binding_match,
            evidence.page_sealed,
        )
    ):
        raise SessionInitializationError("bootstrap evidence was incomplete")
    _assert_login_cleanup(session)
    request_count = (
        session.auth_session_post_count
        + session.device_fingerprint_post_count
        + session.natural_request_observed_total()
    )
    print("GAME=hsr")
    print("OPERATION=bootstrap-check")
    print("STATUS=PASS")
    print("STABLE_CODE=PASS")
    print(f"REQUEST_COUNT={request_count}")
    print("NO_OP=YES")
    print("DATA_MODIFIED=NO")
    print("COMMIT_SHA_PRESENT=NO")


def _assert_formal_unchanged(repo_root: Path, before: str) -> None:
    if tree_digest(repo_root / "data") != before:
        raise SyncError("read-only mode modified formal repository data")


async def run_sync(
    args: argparse.Namespace,
    *,
    snapshot_store: SnapshotStore | None = None,
    metrics: SyncMetrics | None = None,
    request_gate_override: RequestGate | None = None,
    evidence: SyncEvidence | None = None,
) -> int:
    sync_evidence = evidence if evidence is not None else SyncEvidence()
    repo_root = await run_blocking(_repo_root, args.repo_root)
    login_check_only = bool(getattr(args, "login_check_only", False))
    browser_launch_check_only = bool(getattr(args, "browser_launch_check_only", False))
    bootstrap_check_only = bool(getattr(args, "bootstrap_check_only", False))
    await run_blocking(prevalidate_repository, repo_root)
    request_gate = request_gate_override
    browser_profile = resolve_browser_profile(repo_root, args.browser_profile)
    before = tree_digest(repo_root / "data")
    if browser_launch_check_only:
        assert browser_profile is not None
        network_request_count = -1
        async with persistent_browser(
            repo_root=repo_root,
            browser_profile=browser_profile,
            headed=args.headed,
            browser_channel=args.browser_channel,
            launch_check_only=True,
        ) as session:
            network_request_count = session.network_request_count
        if network_request_count != 0:
            raise BrowserLaunchFailedOther("browser launch category: unexpected network activity")
        diagnostics = session.launch_diagnostics
        if diagnostics is None:
            raise BrowserLaunchFailedOther("browser launch category: diagnostics unavailable")
        if diagnostics.active_profile_process_after_failure:
            raise BrowserProfileInUse("browser profile category: in use after launch check")
        if diagnostics.profile_singleton_after_failure:
            raise BrowserProfileStaleSingleton(
                "browser profile category: singleton present after launch check"
            )
        _assert_formal_unchanged(repo_root, before)
        print("BROWSER_LAUNCH_CHECK=PASS")
        print(f"BROWSER_CHANNEL={args.browser_channel}")
        print(f"BROWSER_PROFILE_ID_HASH={digest_prefix(str(browser_profile))}")
        print("NETWORK_REQUEST_COUNT=0")
        print("ACTIVE_PROFILE_PROCESS_AFTER_CHECK=NO")
        print("STALE_SINGLETON_AFTER_CHECK=NO")
        print("POST_CLOSE_PROFILE_PROCESS_COUNT=0")
        print("POST_CLOSE_SINGLETON_COUNT=0")
        print("FORMAL_DATA_MODIFIED=NO")
        return 0

    browser_context: AbstractAsyncContextManager[BrowserSession]
    assert browser_profile is not None
    browser_context = persistent_browser(
        repo_root=repo_root,
        browser_profile=browser_profile,
        headed=args.headed,
        browser_channel=args.browser_channel,
    )
    if not login_check_only and not bootstrap_check_only:
        await run_blocking(
            assert_no_stale_candidates,
            **({"candidate_base": snapshot_store.candidate_base} if snapshot_store else {}),
        )
    dry_run = snapshot_store is None
    collector: OfficialCollector | None = None
    try:
        async with browser_context as session:
            session.request_gate = request_gate
            collector = OfficialCollector(
                session.context,
                request_interval=args.request_interval,
                request_gate=request_gate,
            )
            binding_provider: Callable[[], Awaitable[dict[str, Any]]] | None = None
            if login_check_only:
                if binding_provider is not None:
                    await binding_provider()
                else:
                    await _binding_with_login(
                        collector,
                        session,
                        repo_root=repo_root,
                        uid=args.uid,
                        login_wait_seconds=args.login_wait_seconds,
                    )
                _assert_login_cleanup(session)
                _assert_formal_unchanged(repo_root, before)
                print("GAME=hsr")
                print("OPERATION=login-check")
                print("STATUS=PASS")
                print("STABLE_CODE=PASS")
                return 0
            bootstrap_evidence = await _initialize_session(
                collector,
                session,
                uid=args.uid,
                login_wait_seconds=args.login_wait_seconds,
                binding_provider=binding_provider,
                repo_root=repo_root,
            )
            sync_evidence.exact_binding = bootstrap_evidence.exact_binding
            sync_evidence.expected_region_match = bootstrap_evidence.post_bootstrap_binding_match
            sync_evidence.cultivation_session_bootstrap = all(
                (
                    bootstrap_evidence.cultivation_page_opened,
                    bootstrap_evidence.required_device_headers_captured,
                    bootstrap_evidence.post_bootstrap_binding_match,
                )
            )
            sync_evidence.session_transport_ready = bootstrap_evidence.session_transport_ready
            sync_evidence.page_sealed = bootstrap_evidence.page_sealed
            if bootstrap_check_only:
                _assert_formal_unchanged(repo_root, before)
                sync_evidence.business_pass_count = 0
                sync_evidence.formal_data_unchanged = True
                _bootstrap_summary(bootstrap_evidence, session)
                return 0
            formal_profile, formal_roster, formal_builds = load_formal(repo_root)
            emit("COLLECTING_1")
            first_observation = await _pass(
                collector,
                session,
                uid=args.uid,
            )
            emit("COLLECTING_2")
            second_observation = await _pass(
                collector,
                session,
                uid=args.uid,
            )
            _assert_two_pass_observations_stable(
                first_observation,
                second_observation,
            )
            sync_evidence.business_pass_count = 2
            sync_evidence.profile_coverage = "OBSERVED"
            sync_evidence.roster_coverage = second_observation.roster_coverage
            sync_evidence.build_coverage = second_observation.build_coverage
            sync_evidence.roster_coverage_equal = True
            sync_evidence.avatar_list_diagnostics_equal = True
            sync_evidence.source_coverage_digest_equal = True
            stable_timestamp = datetime(2000, 1, 1, tzinfo=UTC)
            first = materialize_snapshot(
                formal_profile,
                formal_roster,
                formal_builds,
                first_observation,
                synced_at=stable_timestamp,
            )
            second = materialize_snapshot(
                formal_profile,
                formal_roster,
                formal_builds,
                second_observation,
                synced_at=stable_timestamp,
            )
            assert_stable(first, second)
            sync_evidence.business_digest_equal = True
            snapshot = materialize_snapshot(
                formal_profile,
                formal_roster,
                formal_builds,
                second_observation,
                synced_at=datetime.now(UTC),
            )
            if snapshot.profile["sync"]["state"] != "partial":
                raise SyncError("HSR Snapshot state was not partial")
            sync_evidence.snapshot_state = "partial"
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
                    sync_evidence.candidate_validator = "PASS"
                    if snapshot_store is not None:
                        emit("SAVING")
                        await run_blocking(snapshot_store.commit, candidate.game_dir)

                else:
                    sync_evidence.candidate_validator = "SKIPPED_NO_OP"
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
                sync_evidence.candidate_removed = candidate is None or not candidate.root.exists()
            if dry_run:
                _assert_formal_unchanged(repo_root, before)
                sync_evidence.formal_data_unchanged = True
            modified = not dry_run and not lifecycle.no_op
            if metrics is not None:
                metrics.no_op = lifecycle.no_op
                metrics.data_modified = modified
            _summary(
                pass_1_requests=first_observation.request_count,
                pass_2_requests=second_observation.request_count,
                lifecycle=lifecycle,
                modified=modified,
            )
    finally:
        if metrics is not None and collector is not None:
            metrics.request_count = collector.request_count
    return 0
