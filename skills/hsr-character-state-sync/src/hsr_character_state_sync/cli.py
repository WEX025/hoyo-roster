"""Safe-default CLI for HSR partial synchronization."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    asynccontextmanager,
    nullcontext,
)
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hoyo_browser_session import (
    JobParameters,
    mark_login_verified,
    mark_login_waiting,
    submit_and_wait,
)
from hoyo_browser_session.blocking import blocking_context, run_blocking
from hoyo_browser_session.browser import (
    BorrowedBrowserContext,
    BorrowedBrowserSession,
    borrowed_job_page,
)
from hoyo_browser_session.config import Settings, production_settings
from hoyo_browser_session.errors import BrokerError, ContextNotReusable
from hoyo_browser_session.errors import public_error as broker_public_error
from hoyo_browser_session.login import coordinate_login, identity_digest
from hoyo_browser_session.models import (
    ExecutorResult,
    Job,
    LoginProbeResult,
    LoginProbeStatus,
    StableCode,
)
from hoyo_browser_session.request_gate import RequestGate
from hoyo_browser_session.user_workspace import SnapshotStore
from hoyo_browser_session.worker_protocol import read_job_request

from .apply import ApplyGuard, inspect_transaction, recover_transaction
from .browser import (
    DEFAULT_BROWSER_PROFILE,
    AvatarListDiagnostics,
    BrowserSession,
    OfficialCollector,
    SessionInitializationState,
    initialize_official_session,
    install_business_guard,
    persistent_browser,
    remove_business_guard,
    resolve_browser_profile,
)
from .candidate import (
    Candidate,
    assert_no_stale_candidates,
    cleanup_candidate,
    create_candidate,
    inspect_candidates,
    prevalidate_repository,
    tree_digest,
)
from .errors import (
    AuthenticationError,
    AuthenticationRequired,
    BrowserLaunchError,
    BrowserLaunchFailedOther,
    BrowserProfileInUse,
    BrowserProfileStaleSingleton,
    ConfigurationError,
    CultivationOwnedRosterEmpty,
    CultivationSessionMismatch,
    ExpectedUidAmbiguous,
    ExpectedUidNotFound,
    RateLimitedError,
    RiskControlError,
    RosterCompletenessError,
    SessionInitializationError,
    SnapshotContentDrift,
    SyncError,
    WrongAccount,
)
from .evidence import (
    BOOTSTRAP_OPERATION as HSR_BOOTSTRAP_OPERATION,
)
from .evidence import (
    DRY_RUN_OPERATION as HSR_DRY_RUN_OPERATION,
)
from .evidence import (
    PUBLISH_OPERATION as REAL_OPERATION,
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
from .redaction import Redactor, digest_prefix, public_error
from .snapshot import assert_stable


@dataclass(slots=True)
class SyncMetrics:
    request_count: int = 0
    no_op: bool = False
    data_modified: bool = False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hsr-character-state-sync")
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync", help="collect one validated HSR partial snapshot")
    sync.add_argument("--repo-root", type=Path, required=True)
    sync.add_argument("--browser-profile", type=Path, default=DEFAULT_BROWSER_PROFILE)
    sync.add_argument("--uid", required=True)
    mode = sync.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--login-check-only", action="store_true")
    mode.add_argument("--browser-launch-check-only", action="store_true")
    mode.add_argument("--bootstrap-check-only", action="store_true")
    sync.add_argument("--expected-base-sha")
    browser = sync.add_mutually_exclusive_group()
    browser.add_argument("--headed", dest="headed", action="store_true")
    browser.add_argument("--headless", dest="headed", action="store_false")
    sync.set_defaults(headed=True)
    sync.add_argument(
        "--browser-channel",
        choices=("chromium", "chrome"),
        default="chromium",
    )
    sync.add_argument("--login-wait-seconds", type=float, default=900)
    sync.add_argument("--request-interval", type=float, default=2.0)

    publish = commands.add_parser(
        "publish-main",
        help="run one safe HSR Apply and publish data-only changes to origin/main",
    )
    publish.add_argument("--repo-root", type=Path, required=True)
    publish.add_argument("--uid", required=True)
    publish_browser = publish.add_mutually_exclusive_group()
    publish_browser.add_argument("--headed", dest="headed", action="store_true")
    publish_browser.add_argument("--headless", dest="headed", action="store_false")
    publish.set_defaults(headed=True)
    publish.add_argument("--request-interval", type=float, default=2.0)

    for command_name, help_text in (
        ("broker-bootstrap-check", "verify HSR bootstrap through the Browser Owner"),
        ("broker-dry-run", "run one read-only HSR snapshot through the Browser Owner"),
    ):
        broker_read = commands.add_parser(command_name, help=help_text)
        broker_read.add_argument("--repo-root", type=Path, required=True)
        broker_read.add_argument("--uid", required=True)
        broker_browser = broker_read.add_mutually_exclusive_group()
        broker_browser.add_argument("--headed", dest="headed", action="store_true")
        broker_browser.add_argument("--headless", dest="headed", action="store_false")
        broker_read.set_defaults(headed=True)
        broker_read.add_argument("--request-interval", type=float, default=2.0)

    candidate = commands.add_parser("candidate", help="inspect or clean owned candidates")
    candidate_commands = candidate.add_subparsers(dest="candidate_action", required=True)
    candidate_commands.add_parser("inspect")
    cleanup = candidate_commands.add_parser("cleanup")
    cleanup.add_argument("--run-id", required=True)

    transaction = commands.add_parser("transaction", help="inspect or recover Apply residue")
    transaction.add_argument("--repo-root", type=Path, required=True)
    transaction_commands = transaction.add_subparsers(dest="transaction_action", required=True)
    transaction_commands.add_parser("inspect")
    transaction_commands.add_parser("recover")
    return parser


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


def _binding_identity(uid: str) -> str:
    return identity_digest(
        game="hsr",
        game_biz="hkrpg_cn",
        uid=uid,
        region="prod_gf_cn",
    )


class HsrBindingProbe:
    """Read-only exact HSR Binding check for the shared Profile."""

    def __init__(self, collector: OfficialCollector, *, uid: str) -> None:
        self._collector = collector
        self._uid = uid
        self.binding: dict[str, Any] | None = None

    async def check(self) -> LoginProbeResult:
        try:
            binding_data = await self._collector.binding()
        except AuthenticationError:
            return LoginProbeResult(LoginProbeStatus.AUTH_REQUIRED)
        except RateLimitedError:
            return LoginProbeResult(LoginProbeStatus.RATE_LIMITED)
        except RiskControlError:
            return LoginProbeResult(LoginProbeStatus.RISK_CONTROL)
        try:
            binding = select_binding(binding_data, self._uid)
        except (ExpectedUidAmbiguous, ExpectedUidNotFound):
            return LoginProbeResult(LoginProbeStatus.WRONG_ACCOUNT)
        self.binding = binding
        return LoginProbeResult(
            LoginProbeStatus.PASS,
            identity=_binding_identity(binding["uid"]),
        )


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

    def record_waiting() -> None:
        assert job_id is not None
        mark_login_waiting(job_id)
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
        waiting_callback=print_waiting if job_id is None else record_waiting,
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
    borrowed_session: BrowserSession | None = None,
    request_gate_override: RequestGate | None = None,
    evidence: SyncEvidence | None = None,
) -> int:
    sync_evidence = evidence if evidence is not None else SyncEvidence()
    if snapshot_store is not None and (args.apply or borrowed_session is not None):
        raise ConfigurationError("local storage cannot use publication authority")
    repo_root = await run_blocking(_repo_root, args.repo_root)
    apply = bool(args.apply)
    login_check_only = bool(getattr(args, "login_check_only", False))
    browser_launch_check_only = bool(getattr(args, "browser_launch_check_only", False))
    bootstrap_check_only = bool(getattr(args, "bootstrap_check_only", False))
    if apply and not args.expected_base_sha:
        raise ConfigurationError("--expected-base-sha is required with --apply")
    if not apply and args.expected_base_sha:
        raise ConfigurationError("--expected-base-sha is valid only with --apply")
    await run_blocking(prevalidate_repository, repo_root)
    if (borrowed_session is None) != (request_gate_override is None):
        raise ConfigurationError("borrowed browser authority was incomplete")
    borrowed_owner = borrowed_session is not None
    if borrowed_owner and browser_launch_check_only:
        raise ConfigurationError("Owner browser launch checks use hoyo-browser-session")
    request_gate = request_gate_override
    browser_profile = (
        None if borrowed_owner else resolve_browser_profile(repo_root, args.browser_profile)
    )
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
    if borrowed_session is not None:

        @asynccontextmanager
        async def borrowed_context() -> AsyncIterator[BrowserSession]:
            yield borrowed_session

        browser_context = borrowed_context()
    else:
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
    dry_run = not apply and snapshot_store is None
    guard: AbstractContextManager[ApplyGuard | None]
    guard = (
        ApplyGuard(repo_root, expected_base_sha=args.expected_base_sha)
        if apply
        else nullcontext(None)
    )
    collector: OfficialCollector | None = None
    try:
        async with blocking_context(guard) as apply_guard, browser_context as session:
            collector = OfficialCollector(
                session.context,
                request_interval=args.request_interval,
                request_gate=request_gate,
            )
            binding_provider: Callable[[], Awaitable[dict[str, Any]]] | None = None
            if borrowed_owner:

                async def shared_binding_provider() -> dict[str, Any]:
                    return await _binding_with_shared_login(
                        collector,
                        session,
                        repo_root=repo_root,
                        uid=args.uid,
                        job_id=getattr(args, "job_id", None),
                        timeout_seconds=args.login_wait_seconds,
                    )

                binding_provider = shared_binding_provider
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
                if borrowed_owner:
                    mark_login_verified()
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
            first_observation = await _pass(
                collector,
                session,
                uid=args.uid,
            )
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
            lifecycle = evaluate_lifecycle(repo_root, snapshot)
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
                    sync_evidence.candidate_validator = "PASS"
                    if snapshot_store is not None:
                        await run_blocking(snapshot_store.commit, candidate.game_dir)
                    elif not dry_run:
                        if not isinstance(apply_guard, ApplyGuard):
                            raise ConfigurationError("Apply guard was unavailable")
                        await run_blocking(apply_guard.commit, candidate)
                else:
                    sync_evidence.candidate_validator = "SKIPPED_NO_OP"
            finally:
                if candidate is not None:
                    await run_blocking(candidate.cleanup)
                sync_evidence.candidate_removed = True
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


def _candidate_command(args: argparse.Namespace) -> int:
    if args.candidate_action == "inspect":
        inspections = inspect_candidates()
        if not inspections:
            print("CANDIDATE_STATUS=CLEAN")
            return 0
        for inspection in inspections:
            print(f"CANDIDATE_RUN_ID={inspection.run_id}")
            print(f"CANDIDATE_STATUS={inspection.status}")
            print(f"CANDIDATE_CLEANUP_ALLOWED={'YES' if inspection.cleanup_allowed else 'NO'}")
        return 0
    cleanup_candidate(args.run_id)
    print(f"CANDIDATE_RUN_ID={args.run_id}")
    print("CANDIDATE_STATUS=CLEANED")
    return 0


def _transaction_command(args: argparse.Namespace) -> int:
    root = _repo_root(args.repo_root)
    inspection = (
        recover_transaction(root)
        if args.transaction_action == "recover"
        else inspect_transaction(root)
    )
    print(f"TRANSACTION_STATUS={inspection.status}")
    print(f"TRANSACTION_ID={inspection.transaction_id or 'NONE'}")
    print(f"TRANSACTION_VALIDATOR={'PASS' if inspection.validator_pass else 'FAIL'}")
    return 0 if inspection.validator_pass else 1


def _print_owner_read_result(result: Any, *, operation: str) -> int:
    if result.operation != operation:
        raise SyncError("Owner read result operation was inconsistent")
    print("GAME=hsr")
    print(f"OPERATION={operation}")
    print(f"STATUS={result.status.value}")
    print(f"STABLE_CODE={result.stable_code.value}")
    print(f"REQUEST_COUNT={result.request_count}")
    if result.stable_code is not StableCode.PASS:
        print("NO_OP=UNKNOWN")
        print("DATA_MODIFIED=UNKNOWN")
        print("COMMIT_SHA_PRESENT=UNKNOWN")
        return 1
    print(f"NO_OP={'YES' if result.no_op else 'NO'}")
    print("DATA_MODIFIED=NO")
    print("COMMIT_SHA_PRESENT=NO")
    return 0


async def _run_shared_read_operation(
    args: argparse.Namespace,
    *,
    operation: str,
) -> int:
    repo_root = _repo_root(args.repo_root)
    result = submit_and_wait(
        game="hsr",
        operation=operation,
        repo_root=repo_root,
        parameters=JobParameters(
            uid=args.uid,
            headed=args.headed,
            request_interval=args.request_interval,
        ),
        force=True,
    )
    return _print_owner_read_result(result, operation=operation)


async def _run_publish_main(args: argparse.Namespace) -> int:
    repo_root = _repo_root(args.repo_root)
    result = submit_and_wait(
        game="hsr",
        operation=REAL_OPERATION,
        repo_root=repo_root,
        parameters=JobParameters(
            uid=args.uid,
            headed=args.headed,
            request_interval=args.request_interval,
        ),
    )
    print("GAME=hsr")
    print("OPERATION=publish-main")
    print(f"STATUS={result.status.value}")
    print(f"STABLE_CODE={result.stable_code.value}")
    print(f"REQUEST_COUNT={result.request_count}")
    if result.stable_code is StableCode.PUBLISH_OUTCOME_UNKNOWN:
        print("NO_OP=UNKNOWN")
        print("DATA_MODIFIED=UNKNOWN")
        print("COMMIT_SHA_PRESENT=UNKNOWN")
    else:
        print(f"NO_OP={'YES' if result.no_op else 'NO'}")
        print(f"DATA_MODIFIED={'YES' if result.data_modified else 'NO'}")
        print(f"COMMIT_SHA_PRESENT={'YES' if result.commit_sha else 'NO'}")
    return 0 if result.stable_code is StableCode.PASS else 1


async def _run_owner_publish_once(
    args: argparse.Namespace,
    *,
    uid: str,
    headed: bool,
    request_interval: float,
    metrics: SyncMetrics,
    borrowed_session: BrowserSession | None = None,
    request_gate: RequestGate | None = None,
) -> Any:
    from .publisher import publish_main

    async def run_one_apply(worktree: Path, expected_base_sha: str) -> int:
        sync_args = argparse.Namespace(
            command="sync",
            repo_root=worktree,
            uid=uid,
            dry_run=False,
            apply=True,
            login_check_only=False,
            browser_launch_check_only=False,
            bootstrap_check_only=False,
            expected_base_sha=expected_base_sha,
            headed=headed,
            browser_channel="chromium",
            login_wait_seconds=900,
            request_interval=request_interval,
            job_id=args.job_id,
        )
        if borrowed_session is None or request_gate is None:
            raise ConfigurationError("Owner publish requires a Browser Owner borrowed session")
        return await run_sync(
            sync_args,
            metrics=metrics,
            borrowed_session=borrowed_session,
            request_gate_override=request_gate,
        )

    return await publish_main(args.repo_root, sync_runner=run_one_apply)


async def _cleanup_owner_job_session(session: BrowserSession) -> None:
    try:
        await remove_business_guard(session)
    except asyncio.CancelledError:
        if isinstance(session.context, BorrowedBrowserContext):
            session.context._record_cleanup_failure()
        raise
    except BaseException:
        if isinstance(session.context, BorrowedBrowserContext):
            session.context._record_cleanup_failure()
            return
        raise ContextNotReusable("HSR Job browser cleanup failed") from None
    if (
        session.business_guard_installed
        or session._business_route_handler is not None
        or session._business_response_observer is not None
        or session._response_tasks
    ):
        if isinstance(session.context, BorrowedBrowserContext):
            session.context._record_cleanup_failure()
            return
        raise ContextNotReusable("HSR Job browser cleanup left residue")


async def execute_owner_publish_job(
    *,
    job: Job,
    settings: Settings,
    browser: BorrowedBrowserSession,
    request_gate: RequestGate,
) -> ExecutorResult:
    """Run the fixed Publisher with per-Job HSR guards on the Owner Context."""

    metrics = SyncMetrics()
    try:
        parameters = read_job_request(
            settings,
            job_id=job.job_id,
            game="hsr",
            operation=REAL_OPERATION,
            expected_digest=job.parameter_digest,
        )
        if parameters.uid is None:
            raise ConfigurationError("internal HSR UID was missing")
        async with borrowed_job_page(browser) as page:
            borrowed = BrowserSession(
                playwright=None,
                context=browser.context,
                page=page,
                headed=True,
                request_gate=request_gate,
                requested_min_interval=parameters.request_interval,
            )
            try:
                publish_result = await _run_owner_publish_once(
                    argparse.Namespace(
                        repo_root=job.repo_root,
                        job_id=job.job_id,
                    ),
                    uid=parameters.uid,
                    headed=True,
                    request_interval=parameters.request_interval,
                    metrics=metrics,
                    borrowed_session=borrowed,
                    request_gate=request_gate,
                )
            finally:
                await _cleanup_owner_job_session(borrowed)
        commit_missing = publish_result.commit_sha is None and not publish_result.no_op
        commit_unexpected = publish_result.commit_sha is not None and publish_result.no_op
        if (
            publish_result.no_op != metrics.no_op
            or metrics.data_modified != (not publish_result.no_op)
            or commit_missing
            or commit_unexpected
        ):
            raise SyncError("Owner publish result was inconsistent")
        return ExecutorResult(
            stable_code=StableCode.PASS,
            request_count=metrics.request_count,
            no_op=publish_result.no_op,
            data_modified=not publish_result.no_op,
            commit_sha=publish_result.commit_sha,
        )
    except asyncio.CancelledError:
        raise
    except SyncError as error:
        return ExecutorResult(
            stable_code=_worker_failure_code(error),
            request_count=metrics.request_count,
        )


async def _execute_owner_read_job(
    *,
    job: Job,
    settings: Settings,
    browser: BorrowedBrowserSession,
    request_gate: RequestGate,
    operation: str,
) -> ExecutorResult:
    metrics = SyncMetrics()
    evidence = SyncEvidence()
    try:
        parameters = read_job_request(
            settings,
            job_id=job.job_id,
            game="hsr",
            operation=operation,
            expected_digest=job.parameter_digest,
        )
        if parameters.uid is None:
            raise ConfigurationError("internal HSR UID was missing")
        async with borrowed_job_page(browser) as page:
            borrowed = BrowserSession(
                playwright=None,
                context=browser.context,
                page=page,
                headed=False,
                request_gate=request_gate,
                requested_min_interval=parameters.request_interval,
            )
            try:
                result = await run_sync(
                    argparse.Namespace(
                        command="sync",
                        repo_root=job.repo_root,
                        uid=parameters.uid,
                        dry_run=operation == HSR_DRY_RUN_OPERATION,
                        apply=False,
                        login_check_only=False,
                        browser_launch_check_only=False,
                        bootstrap_check_only=(operation == HSR_BOOTSTRAP_OPERATION),
                        expected_base_sha=None,
                        headed=False,
                        browser_channel="chromium",
                        login_wait_seconds=0,
                        request_interval=parameters.request_interval,
                        job_id=job.job_id,
                    ),
                    metrics=metrics,
                    borrowed_session=borrowed,
                    request_gate_override=request_gate,
                    evidence=evidence,
                )
            finally:
                await _cleanup_owner_job_session(borrowed)
        if result != 0 or metrics.data_modified:
            raise SyncError("Owner read-only result was inconsistent")
        no_op = True if operation == HSR_BOOTSTRAP_OPERATION else metrics.no_op
        evidence_codes = evidence.evidence_codes(operation)
        return ExecutorResult(
            stable_code=StableCode.PASS,
            request_count=metrics.request_count,
            no_op=no_op,
            data_modified=False,
            commit_sha=None,
            evidence_codes=evidence_codes,
        )
    except asyncio.CancelledError:
        raise
    except SyncError as error:
        return ExecutorResult(
            stable_code=_worker_failure_code(error),
            request_count=metrics.request_count,
        )


async def execute_owner_bootstrap_job(
    *,
    job: Job,
    settings: Settings,
    browser: BorrowedBrowserSession,
    request_gate: RequestGate,
) -> ExecutorResult:
    return await _execute_owner_read_job(
        job=job,
        settings=settings,
        browser=browser,
        request_gate=request_gate,
        operation=HSR_BOOTSTRAP_OPERATION,
    )


async def execute_owner_dry_run_job(
    *,
    job: Job,
    settings: Settings,
    browser: BorrowedBrowserSession,
    request_gate: RequestGate,
) -> ExecutorResult:
    return await _execute_owner_read_job(
        job=job,
        settings=settings,
        browser=browser,
        request_gate=request_gate,
        operation=HSR_DRY_RUN_OPERATION,
    )


def _worker_failure_code(error: SyncError) -> StableCode:
    if isinstance(error, RateLimitedError):
        return StableCode.RATE_LIMITED
    if isinstance(error, RiskControlError):
        return StableCode.RISK_CONTROL
    if isinstance(error, ExpectedUidAmbiguous | ExpectedUidNotFound | WrongAccount):
        return StableCode.WRONG_ACCOUNT
    if isinstance(error, AuthenticationError | AuthenticationRequired):
        return StableCode.AUTHENTICATION_REQUIRED
    if isinstance(error, BrowserProfileInUse):
        return StableCode.PROFILE_IN_USE
    if isinstance(error, BrowserProfileStaleSingleton):
        return StableCode.PROFILE_STALE_SINGLETON
    if isinstance(error, BrowserLaunchError):
        return StableCode.OWNER_BROKEN
    return StableCode.JOB_FAILED


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = getattr(args, "repo_root", None)
    sensitive_values = [getattr(args, "uid", "")]
    if isinstance(repo_root, Path):
        sensitive_values.extend((str(repo_root), str(repo_root.expanduser().absolute())))
    redactor = Redactor(tuple(sensitive_values))
    try:
        if args.command == "candidate":
            return _candidate_command(args)
        if args.command == "transaction":
            return _transaction_command(args)
        if args.command == "publish-main":
            return asyncio.run(_run_publish_main(args))
        if args.command == "broker-bootstrap-check":
            return asyncio.run(
                _run_shared_read_operation(
                    args,
                    operation=HSR_BOOTSTRAP_OPERATION,
                )
            )
        if args.command == "broker-dry-run":
            return asyncio.run(
                _run_shared_read_operation(
                    args,
                    operation=HSR_DRY_RUN_OPERATION,
                )
            )
        return asyncio.run(run_sync(args))
    except KeyboardInterrupt:
        label = "PUBLISH_MAIN=FAIL" if args.command == "publish-main" else "SYNC=FAIL"
        print(label, file=sys.stderr)
        print("ERROR_CODE=INTERRUPTED", file=sys.stderr)
        return 130
    except Exception as error:
        code, message = (
            broker_public_error(error) if isinstance(error, BrokerError) else public_error(error)
        )
        label = "PUBLISH_MAIN=FAIL" if args.command == "publish-main" else "SYNC=FAIL"
        print(label, file=sys.stderr)
        print(f"ERROR_CODE={code}", file=sys.stderr)
        print(f"ERROR={redactor.redact(message)}", file=sys.stderr)
        return 1
