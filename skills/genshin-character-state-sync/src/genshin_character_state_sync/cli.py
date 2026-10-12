"""Safe-default CLI for Genshin partial synchronization."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    asynccontextmanager,
    nullcontext,
)
from dataclasses import dataclass
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
    BorrowedBrowserSession,
    borrowed_job_page,
)
from hoyo_browser_session.config import REAL_OPERATION, Settings, production_settings
from hoyo_browser_session.login import coordinate_login
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
    BrowserSession,
    OfficialCollector,
    persistent_browser,
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
    ApiResponseError,
    AuthenticationError,
    CandidateValidationError,
    ConfigurationError,
    ExpectedUidAmbiguous,
    ExpectedUidNotFound,
    LifecycleRegression,
    LoginRequired,
    OfficialRequestNotObserved,
    PartialFieldCoverageRegression,
    RateLimitedError,
    RiskControlError,
    RosterCompletenessError,
    SnapshotConsistencyError,
    SnapshotContentDrift,
    SyncError,
    UnsupportedMultiPageContract,
)
from .lifecycle import LifecycleDecision, evaluate_lifecycle
from .login_provider import create_routine_login_provider
from .model import Snapshot
from .normalize import (
    normalize_snapshot,
    select_binding,
    validate_complete_list,
)
from .redaction import Redactor, public_error
from .snapshot import assert_stable


@dataclass(slots=True)
class SyncMetrics:
    request_count: int = 0
    no_op: bool = False
    data_modified: bool = False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="genshin-character-state-sync")
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync", help="collect one validated Genshin partial snapshot")
    sync.add_argument("--repo-root", type=Path, required=True)
    sync.add_argument("--browser-profile", type=Path, default=DEFAULT_BROWSER_PROFILE)
    sync.add_argument("--uid", required=True)
    mode = sync.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    sync.add_argument("--expected-base-sha")
    browser = sync.add_mutually_exclusive_group()
    browser.add_argument("--headed", dest="headed", action="store_true")
    browser.add_argument("--headless", dest="headed", action="store_false")
    sync.set_defaults(headed=True)
    sync.add_argument("--request-interval", type=float, default=1.8)
    sync.add_argument("--login-check-only", action="store_true")

    publish = commands.add_parser(
        "publish-main",
        help="run one safe Apply and publish Genshin data to origin/main",
    )
    publish.add_argument("--repo-root", type=Path, required=True)
    publish.add_argument("--uid", required=True)
    publish_browser = publish.add_mutually_exclusive_group()
    publish_browser.add_argument("--headed", dest="headed", action="store_true")
    publish_browser.add_argument("--headless", dest="headed", action="store_false")
    publish.set_defaults(headed=True)
    publish.add_argument("--request-interval", type=float, default=1.8)

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
        root / "data/genshin-impact/profile.json",
        root / "schemas/genshin-impact/build.schema.json",
        root / "tools/validate_repository.py",
    )
    if not all(path.is_file() and not path.is_symlink() for path in required):
        raise ConfigurationError("repo-root does not contain the repository contract")
    return root


def _binding_identity(uid: str) -> str:
    return hashlib.sha256(f"genshin:hk4e_cn:{uid}".encode()).hexdigest()


class GenshinBindingProbe:
    def __init__(self, collector: OfficialCollector, *, uid: str) -> None:
        self._collector = collector
        self._uid = uid
        self.binding_data: dict[str, Any] | None = None

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
        except (ExpectedUidNotFound, ExpectedUidAmbiguous):
            return LoginProbeResult(LoginProbeStatus.WRONG_ACCOUNT)
        self.binding_data = binding_data
        return LoginProbeResult(
            LoginProbeStatus.PASS,
            identity=_binding_identity(binding["uid"]),
        )


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

    def record_waiting() -> None:
        assert job_id is not None
        mark_login_waiting(job_id)

    waiting_callback: Callable[[], None] = print_waiting if job_id is None else record_waiting
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
    borrowed_session: BrowserSession | None = None,
    request_gate_override: RequestGate | None = None,
) -> int:
    if snapshot_store is not None and (args.apply or borrowed_session is not None):
        raise ConfigurationError("local storage cannot use publication authority")
    repo_root = await run_blocking(_repo_root, args.repo_root)
    await run_blocking(
        assert_no_stale_candidates,
        **({"candidate_base": snapshot_store.candidate_base} if snapshot_store else {}),
    )
    await run_blocking(prevalidate_repository, repo_root)
    if (borrowed_session is None) != (request_gate_override is None):
        raise ConfigurationError("borrowed browser authority was incomplete")
    request_gate = request_gate_override
    browser_context: AbstractAsyncContextManager[BrowserSession]
    if borrowed_session is not None:

        @asynccontextmanager
        async def borrowed_context() -> AsyncIterator[BrowserSession]:
            yield borrowed_session

        browser_context = borrowed_context()
    else:
        browser_profile = resolve_browser_profile(repo_root, args.browser_profile)
        browser_context = persistent_browser(
            repo_root=repo_root,
            browser_profile=browser_profile,
            headed=args.headed,
        )
    if args.apply and not args.expected_base_sha:
        raise ConfigurationError("--expected-base-sha is required with --apply")
    dry_run = not args.apply and snapshot_store is None
    before = tree_digest(repo_root / "data")
    guard: AbstractContextManager[ApplyGuard | None]
    guard = (
        ApplyGuard(repo_root, expected_base_sha=args.expected_base_sha)
        if args.apply
        else nullcontext(None)
    )
    collector: OfficialCollector | None = None
    try:
        async with blocking_context(guard) as apply_guard, browser_context as session:
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
                if borrowed_session is not None:
                    mark_login_verified()
                print("GAME=genshin")
                print("OPERATION=login-check")
                print("STATUS=PASS")
                print("STABLE_CODE=PASS")
                return 0
            first, first_requests_after_binding = await _pass(
                collector,
                session,
                repo_root=repo_root,
                uid=args.uid,
                job_id=job_id,
                request_gate=request_gate,
                request_interval=args.request_interval,
            )
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
                    if snapshot_store is not None:
                        await run_blocking(snapshot_store.commit, candidate.game_dir)
                    elif not dry_run:
                        if not isinstance(apply_guard, ApplyGuard):
                            raise ConfigurationError("Apply guard was unavailable")
                        await run_blocking(apply_guard.commit, candidate)
            finally:
                if candidate is not None:
                    await run_blocking(candidate.cleanup)
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


async def _run_publish_main(args: argparse.Namespace) -> int:
    repo_root = _repo_root(args.repo_root)
    result = submit_and_wait(
        game="genshin",
        operation=REAL_OPERATION,
        repo_root=repo_root,
        parameters=JobParameters(
            uid=args.uid,
            headed=args.headed,
            request_interval=args.request_interval,
        ),
    )
    print("GAME=genshin")
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
            expected_base_sha=expected_base_sha,
            headed=headed,
            request_interval=request_interval,
            login_check_only=False,
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


async def execute_owner_job(
    *,
    job: Job,
    settings: Settings,
    browser: BorrowedBrowserSession,
    request_gate: RequestGate,
) -> ExecutorResult:
    """Run the fixed Publisher with the Owner's live browser Context."""

    metrics = SyncMetrics()
    try:
        parameters = read_job_request(
            settings,
            job_id=job.job_id,
            game="genshin",
            operation=REAL_OPERATION,
            expected_digest=job.parameter_digest,
        )
        if parameters.uid is None:
            raise ConfigurationError("internal Genshin UID was missing")
        async with borrowed_job_page(browser) as page:
            borrowed = BrowserSession(
                playwright=None,
                context=browser.context,
                page=page,
                headed=True,
            )
            publish_result = await _run_owner_publish_once(
                argparse.Namespace(repo_root=job.repo_root, job_id=job.job_id),
                uid=parameters.uid,
                headed=True,
                request_interval=parameters.request_interval,
                metrics=metrics,
                borrowed_session=borrowed,
                request_gate=request_gate,
            )
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


def _worker_failure_code(error: SyncError) -> StableCode:
    if isinstance(error, RateLimitedError):
        return StableCode.RATE_LIMITED
    if isinstance(error, RiskControlError):
        return StableCode.RISK_CONTROL
    if isinstance(error, ExpectedUidNotFound | ExpectedUidAmbiguous):
        return StableCode.WRONG_ACCOUNT
    if isinstance(error, AuthenticationError | LoginRequired):
        return StableCode.AUTHENTICATION_REQUIRED
    # Only fixed categories cross the Owner boundary; never inspect exception text.
    if isinstance(error, LifecycleRegression):
        for categories, code in (
            (("uid", "server"), StableCode.GENSHIN_IDENTITY_CHANGED),
            (("roster_shrink",), StableCode.GENSHIN_ROSTER_SHRANK),
            (("player_level",), StableCode.GENSHIN_PLAYER_LEVEL_DECREASED),
            (("friendship_level",), StableCode.GENSHIN_FRIENDSHIP_DECREASED),
            (("character_progression",), StableCode.GENSHIN_CHARACTER_PROGRESSION_DECREASED),
            (("synced_at",), StableCode.GENSHIN_TIMESTAMP_REGRESSED),
        ):
            if any(category in error.categories for category in categories):
                if code is StableCode.GENSHIN_CHARACTER_PROGRESSION_DECREASED:
                    for detail, detailed_code in (
                        ("character_level", StableCode.GENSHIN_CHARACTER_LEVEL_DECREASED),
                        ("character_ascension", StableCode.GENSHIN_ASCENSION_DECREASED),
                        ("character_constellation", StableCode.GENSHIN_CONSTELLATION_DECREASED),
                        ("skill_normal_attack_level", StableCode.GENSHIN_NORMAL_ATTACK_DECREASED),
                        (
                            "skill_elemental_skill_level",
                            StableCode.GENSHIN_ELEMENTAL_SKILL_DECREASED,
                        ),
                        (
                            "skill_elemental_burst_level",
                            StableCode.GENSHIN_ELEMENTAL_BURST_DECREASED,
                        ),
                    ):
                        if detail in error.details:
                            return detailed_code
                return code
        return StableCode.GENSHIN_LIFECYCLE_FAILED
    for error_types, code in (
        ((SnapshotContentDrift,), StableCode.GENSHIN_SNAPSHOT_DRIFT),
        ((PartialFieldCoverageRegression,), StableCode.GENSHIN_FIELD_COVERAGE_REGRESSED),
        ((CandidateValidationError,), StableCode.GENSHIN_CANDIDATE_VALIDATION_FAILED),
        (
            (SnapshotConsistencyError, RosterCompletenessError, UnsupportedMultiPageContract),
            StableCode.GENSHIN_SOURCE_CONTRACT_FAILED,
        ),
        ((ApiResponseError, OfficialRequestNotObserved), StableCode.GENSHIN_SOURCE_RESPONSE_FAILED),
    ):
        if isinstance(error, error_types):
            return code
    return StableCode.JOB_FAILED


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = getattr(args, "repo_root", None)
    sensitive_values = [getattr(args, "uid", "")]
    if isinstance(repo_root, Path):
        sensitive_values.extend((str(repo_root), str(repo_root.expanduser().absolute())))
    redactor = Redactor(sensitive_values)
    try:
        if args.command == "candidate":
            return _candidate_command(args)
        if args.command == "transaction":
            return _transaction_command(args)
        if args.command == "publish-main":
            return asyncio.run(_run_publish_main(args))
        return asyncio.run(run_sync(args))
    except KeyboardInterrupt:
        label = "PUBLISH_MAIN=FAIL" if args.command == "publish-main" else "SYNC=FAIL"
        print(label, file=sys.stderr)
        print("ERROR_CODE=INTERRUPTED", file=sys.stderr)
        return 130
    except Exception as error:
        code, message = public_error(error)
        label = "PUBLISH_MAIN=FAIL" if args.command == "publish-main" else "SYNC=FAIL"
        print(label, file=sys.stderr)
        print(f"ERROR_CODE={code}", file=sys.stderr)
        print(f"ERROR={redactor.redact(message)}", file=sys.stderr)
        return 1
