"""Command-line entrypoint with dry-run and headed mode as safe defaults."""

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

from .apply import (
    ApplyAuthority,
    ApplyGuard,
    inspect_transaction,
    recover_transaction,
)
from .browser import (
    DEFAULT_BROWSER_PROFILE,
    BrowserSession,
    _capture_rpc_headers,
    persistent_browser,
    prime_rpc_headers,
    resolve_browser_profile,
)
from .candidate import (
    Candidate,
    assert_no_stale_candidates,
    cleanup_candidate,
    create_candidate,
    inspect_candidates,
    run_repository_validator,
    tree_digest,
)
from .endpoints import EndpointClient, select_role
from .errors import (
    ApiResponseError,
    ApplyError,
    ApplyPostValidationError,
    AuthenticationError,
    CandidateCleanupError,
    CandidateValidationError,
    ConfigurationError,
    DirectMainPushRejected,
    FormalRepositoryPrevalidationError,
    LifecycleRegressionError,
    LoginRequired,
    MappingError,
    PublishError,
    RateLimitedError,
    RemoteMainMoved,
    RequestTransportError,
    RiskControlError,
    SnapshotConsistencyError,
    SnapshotContentDrift,
    SyncedAtRegressionError,
    SyncError,
    UidRequiredError,
)
from .jsonio import StrictJsonError, require_real_path_chain
from .lifecycle import LifecycleDecision, evaluate_lifecycle
from .login_provider import create_routine_login_provider
from .normalize import normalize_collection
from .redaction import Redactor, public_error
from .snapshot import assert_stable_business_snapshots
from .transport import BrowserTransport


@dataclass(slots=True)
class SyncMetrics:
    request_count: int = 0
    no_op: bool = False
    data_modified: bool = False


# Public phase labels are constants, never derived from an exception message.
_FAILURE_PHASES = {
    StableCode.ZZZ_FORMAL_VALIDATION_FAILED: "FORMAL_PREVALIDATION",
    StableCode.ZZZ_CANDIDATE_VALIDATION_FAILED: "CANDIDATE_VALIDATION",
    StableCode.ZZZ_CANDIDATE_CLEANUP_FAILED: "CANDIDATE_CLEANUP",
    StableCode.ZZZ_APPLY_VALIDATION_FAILED: "APPLY_POST_VALIDATION",
    StableCode.ZZZ_APPLY_FAILED: "APPLY",
    StableCode.ZZZ_PUBLISH_FAILED: "PUBLICATION",
    StableCode.ZZZ_REMOTE_MAIN_MOVED: "PUBLICATION_BASE_CHECK",
    StableCode.ZZZ_MAIN_PUSH_REJECTED: "PUBLICATION_PUSH",
    StableCode.ZZZ_MAPPING_FAILED: "NORMALIZATION",
    StableCode.ZZZ_SNAPSHOT_DRIFT: "BUSINESS_DIGEST_COMPARISON",
    StableCode.ZZZ_LIFECYCLE_FAILED: "LIFECYCLE_COMPARISON",
    StableCode.ZZZ_SOURCE_TRANSPORT_FAILED: "SOURCE_TRANSPORT",
    StableCode.ZZZ_SOURCE_RESPONSE_FAILED: "SOURCE_RESPONSE_VALIDATION",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zzz-character-state-sync")
    subparsers = parser.add_subparsers(dest="command", required=True)
    sync = subparsers.add_parser("sync", help="synchronize current ZZZ character state")
    sync.add_argument("--repo-root", type=Path, required=True)
    sync.add_argument(
        "--browser-profile",
        type=Path,
        default=DEFAULT_BROWSER_PROFILE,
    )
    mode = sync.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    sync.add_argument("--expected-base-sha")
    browser_mode = sync.add_mutually_exclusive_group()
    browser_mode.add_argument("--headed", dest="headed", action="store_true")
    browser_mode.add_argument("--headless", dest="headed", action="store_false")
    sync.set_defaults(headed=True)
    sync.add_argument("--request-interval", type=float, default=1.8)
    sync.add_argument("--uid")
    sync.add_argument(
        "--login-check-only",
        action="store_true",
        help="verify that the persistent login is reusable without collecting account state",
    )
    publish = subparsers.add_parser(
        "publish-main",
        help="run one safe Apply and publish a data-only fast-forward to origin/main",
    )
    publish.add_argument("--repo-root", type=Path, required=True)
    publish_browser_mode = publish.add_mutually_exclusive_group()
    publish_browser_mode.add_argument(
        "--headed",
        dest="headed",
        action="store_true",
    )
    publish_browser_mode.add_argument(
        "--headless",
        dest="headed",
        action="store_false",
    )
    publish.set_defaults(headed=True)
    publish.add_argument("--request-interval", type=float, default=1.8)
    publish.add_argument("--uid")

    candidate = subparsers.add_parser("candidate", help="inspect or clean stale candidates")
    candidate_actions = candidate.add_subparsers(dest="candidate_action", required=True)
    candidate_actions.add_parser("inspect")
    candidate_cleanup = candidate_actions.add_parser("cleanup")
    candidate_cleanup.add_argument("--run-id", required=True)

    transaction = subparsers.add_parser(
        "transaction",
        help="inspect or recover an interrupted apply transaction",
    )
    transaction.add_argument("--repo-root", type=Path, required=True)
    transaction_actions = transaction.add_subparsers(
        dest="transaction_action",
        required=True,
    )
    transaction_actions.add_parser("inspect")
    transaction_actions.add_parser("recover")
    return parser


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


def _binding_identity(uid: str) -> str:
    return hashlib.sha256(f"zzz:nap_cn:{uid}".encode()).hexdigest()


class ZzzBindingProbe:
    def __init__(self, endpoints: EndpointClient, *, uid: str | None) -> None:
        self._endpoints = endpoints
        self._uid = uid
        self.binding_data: dict[str, Any] | None = None

    async def check(self) -> LoginProbeResult:
        try:
            binding_data = dict(await self._endpoints.get_binding())
        except AuthenticationError:
            return LoginProbeResult(LoginProbeStatus.AUTH_REQUIRED)
        except RateLimitedError:
            return LoginProbeResult(LoginProbeStatus.RATE_LIMITED)
        except RiskControlError:
            return LoginProbeResult(LoginProbeStatus.RISK_CONTROL)
        try:
            role = select_role(binding_data, self._uid)
        except UidRequiredError:
            raise
        except ConfigurationError:
            return LoginProbeResult(LoginProbeStatus.WRONG_ACCOUNT)
        self.binding_data = binding_data
        return LoginProbeResult(
            LoginProbeStatus.PASS,
            identity=_binding_identity(role.uid),
        )


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

    def record_waiting() -> None:
        assert job_id is not None
        mark_login_waiting(job_id)

    waiting_callback: Callable[[], None] = print_waiting if job_id is None else record_waiting
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
    apply_authority: ApplyAuthority = ApplyAuthority.LOCAL_MAIN,
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
    await run_blocking(_prevalidate_formal_repository, repo_root)
    if (borrowed_session is None) != (request_gate_override is None):
        raise ConfigurationError("borrowed browser authority was incomplete")
    borrowed_owner = borrowed_session is not None
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
    dry_run = bool(args.dry_run or not args.apply) and snapshot_store is None
    formal_digest_before = tree_digest(repo_root / "data")

    expected_base_sha = getattr(args, "expected_base_sha", None)
    if args.apply and not isinstance(expected_base_sha, str):
        raise ConfigurationError("--expected-base-sha is required with --apply")
    guard_context: AbstractContextManager[ApplyGuard | None]
    if args.apply:
        assert isinstance(expected_base_sha, str)
        if apply_authority is ApplyAuthority.LOCAL_MAIN:
            guard_context = ApplyGuard(
                repo_root,
                expected_base_sha=expected_base_sha,
            )
        else:
            guard_context = ApplyGuard(
                repo_root,
                expected_base_sha=expected_base_sha,
                authority=apply_authority,
            )
    else:
        guard_context = nullcontext(None)
    transport: BrowserTransport | None = None
    try:
        async with (
            blocking_context(guard_context) as apply_guard,
            browser_context as browser_session,
        ):
            job_id = getattr(args, "job_id", None)
            if job_id is not None and not isinstance(job_id, str):
                raise ConfigurationError("internal job ID was invalid")
            if borrowed_owner:
                transport = BrowserTransport(
                    browser_session.context,
                    request_interval=args.request_interval,
                    rpc_headers=browser_session.rpc_headers,
                    request_gate=request_gate,
                )
            else:
                transport = BrowserTransport(
                    browser_session.context,
                    request_interval=args.request_interval,
                    rpc_headers=browser_session.rpc_headers,
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
                if borrowed_owner:
                    mark_login_verified()
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
            first_collection = await endpoints.collect_from_binding(
                binding,
                uid=args.uid,
            )
            pass_1_request_count = transport.request_count - request_count_start
            first_snapshot = normalize_collection(first_collection)
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
            lifecycle = evaluate_lifecycle(repo_root, snapshot)
            if lifecycle.no_op and args.apply:
                if not isinstance(apply_guard, ApplyGuard):
                    raise ConfigurationError("apply guard was not available")
                apply_guard.assert_stable()

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
                        await run_blocking(snapshot_store.commit, candidate.zzz_dir)
                    elif not dry_run:
                        if not isinstance(apply_guard, ApplyGuard):
                            raise ConfigurationError("apply guard was not available")
                        await run_blocking(apply_guard.commit, candidate.zzz_dir)
            finally:
                if candidate is not None:
                    await run_blocking(candidate.cleanup)

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


def _run_candidate_command(args: argparse.Namespace) -> int:
    if args.candidate_action == "inspect":
        inspections = inspect_candidates()
        if not inspections:
            print("CANDIDATE_STATUS=CLEAN")
            return 0
        for inspection in inspections:
            print(f"CANDIDATE_RUN_ID={inspection.run_id}")
            print(f"CANDIDATE_STATUS={inspection.status}")
            allowed = "YES" if inspection.cleanup_allowed else "NO"
            print(f"CANDIDATE_CLEANUP_ALLOWED={allowed}")
        return 0
    cleanup_candidate(args.run_id)
    print(f"CANDIDATE_RUN_ID={args.run_id}")
    print("CANDIDATE_STATUS=CLEANED")
    return 0


def _run_transaction_command(args: argparse.Namespace) -> int:
    repo_root = _repo_root(args.repo_root)
    inspection = (
        recover_transaction(repo_root)
        if args.transaction_action == "recover"
        else inspect_transaction(repo_root)
    )
    print(f"TRANSACTION_STATUS={inspection.status}")
    print(f"TRANSACTION_ID={inspection.transaction_id or 'NONE'}")
    validator = "PASS" if inspection.validator_pass else "FAIL"
    print(f"TRANSACTION_VALIDATOR={validator}")
    return 0 if inspection.validator_pass else 1


async def _run_publish_main(args: argparse.Namespace) -> int:
    repo_root = _repo_root(args.repo_root)
    result = submit_and_wait(
        game="zzz",
        operation=REAL_OPERATION,
        repo_root=repo_root,
        parameters=JobParameters(
            uid=args.uid,
            headed=args.headed,
            request_interval=args.request_interval,
        ),
    )
    print("GAME=zzz")
    print("OPERATION=publish-main")
    print(f"STATUS={result.status.value}")
    print(f"STABLE_CODE={result.stable_code.value}")
    print(f"REQUEST_COUNT={result.request_count}")
    if result.stable_code in _FAILURE_PHASES:
        print(f"FAILURE_STAGE={_FAILURE_PHASES[result.stable_code]}")
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
    uid: str | None,
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
            dry_run=False,
            apply=True,
            expected_base_sha=expected_base_sha,
            headed=headed,
            request_interval=request_interval,
            uid=uid,
            login_check_only=False,
            job_id=args.job_id,
        )
        if borrowed_session is None or request_gate is None:
            raise ConfigurationError("Owner publish requires a Browser Owner borrowed session")
        return await run_sync(
            sync_args,
            apply_authority=ApplyAuthority.ORIGIN_MAIN,
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
            game="zzz",
            operation=REAL_OPERATION,
            expected_digest=job.parameter_digest,
        )
        async with borrowed_job_page(browser) as page:
            borrowed = BrowserSession(
                playwright=None,
                context=browser.context,
                page=page,
                headed=True,
            )
            page.on(
                "request",
                lambda request: _capture_rpc_headers(borrowed, request),
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
    if isinstance(error, UidRequiredError):
        return StableCode.UID_REQUIRED
    if isinstance(error, AuthenticationError | LoginRequired):
        return StableCode.AUTHENTICATION_REQUIRED
    # Preserve only fixed error categories. Never serialize exception text,
    # traceback, endpoint parameters, source values, or authentication state.
    for error_types, code in (
        ((FormalRepositoryPrevalidationError,), StableCode.ZZZ_FORMAL_VALIDATION_FAILED),
        ((CandidateValidationError,), StableCode.ZZZ_CANDIDATE_VALIDATION_FAILED),
        ((CandidateCleanupError,), StableCode.ZZZ_CANDIDATE_CLEANUP_FAILED),
        ((ApplyPostValidationError,), StableCode.ZZZ_APPLY_VALIDATION_FAILED),
        ((ApplyError,), StableCode.ZZZ_APPLY_FAILED),
        ((RemoteMainMoved,), StableCode.ZZZ_REMOTE_MAIN_MOVED),
        ((DirectMainPushRejected,), StableCode.ZZZ_MAIN_PUSH_REJECTED),
        ((PublishError,), StableCode.ZZZ_PUBLISH_FAILED),
        ((MappingError,), StableCode.ZZZ_MAPPING_FAILED),
        ((SnapshotContentDrift,), StableCode.ZZZ_SNAPSHOT_DRIFT),
        ((LifecycleRegressionError, SyncedAtRegressionError), StableCode.ZZZ_LIFECYCLE_FAILED),
        ((RequestTransportError,), StableCode.ZZZ_SOURCE_TRANSPORT_FAILED),
        ((ApiResponseError,), StableCode.ZZZ_SOURCE_RESPONSE_FAILED),
    ):
        if isinstance(error, error_types):
            return code
    return StableCode.JOB_FAILED


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    redactor = Redactor(uid_values=(args.uid,) if getattr(args, "uid", None) else ())
    try:
        if args.command == "candidate":
            return _run_candidate_command(args)
        if args.command == "transaction":
            return _run_transaction_command(args)
        if args.command == "publish-main":
            return asyncio.run(_run_publish_main(args))
        return asyncio.run(run_sync(args))
    except KeyboardInterrupt:
        failure_label = "PUBLISH_MAIN=FAIL" if args.command == "publish-main" else "SYNC=FAIL"
        print(failure_label, file=sys.stderr)
        print("ERROR_CODE=INTERRUPTED", file=sys.stderr)
        return 130
    except Exception as exc:
        code, message = public_error(exc)
        failure_label = "PUBLISH_MAIN=FAIL" if args.command == "publish-main" else "SYNC=FAIL"
        print(failure_label, file=sys.stderr)
        print(f"ERROR_CODE={code}", file=sys.stderr)
        print(f"ERROR={redactor.redact(message)}", file=sys.stderr)
        return 1
