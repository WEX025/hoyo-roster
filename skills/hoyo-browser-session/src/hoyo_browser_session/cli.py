"""Privacy-safe management CLI for the shared local broker foundation."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import cast

from .config import (
    ACCOUNT_SCOPE,
    EXECUTOR_TIMEOUT_SECONDS,
    LOGIN_OPERATION,
    POST_JOB_COOLDOWN_SECONDS,
    RECENT_SUCCESS_REUSE_SECONDS,
    SESSION_GAME,
    SHARED_REQUEST_MIN_INTERVAL_SECONDS,
    SYNTHETIC_DELAY_SECONDS,
    WORKER_HEARTBEAT_SECONDS,
    WORKER_STALE_SECONDS,
    Settings,
    isolated_test_settings,
    production_settings,
)
from .errors import (
    OwnerQueueNotEmpty,
    RuntimeNotReady,
    public_error,
)
from .executors import run_synthetic_child
from .models import JobParameters, StableCode
from .owner import (
    owner_status,
    recover_broken_owner,
    recover_stale_shutdown,
    request_owner_shutdown,
    run_owner,
    run_owner_launch_check,
)
from .profile import ProfileManager
from .queue import JobQueue, submit_and_wait, submit_and_wait_with_settings
from .redaction import Redactor
from .runtime import doctor_summary, initialize_external_state


def _add_test_timing_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--request-interval",
        type=float,
        default=SHARED_REQUEST_MIN_INTERVAL_SECONDS,
    )
    parser.add_argument("--cooldown", type=float, default=POST_JOB_COOLDOWN_SECONDS)
    parser.add_argument("--reuse-window", type=float, default=RECENT_SUCCESS_REUSE_SECONDS)
    parser.add_argument("--heartbeat", type=float, default=WORKER_HEARTBEAT_SECONDS)
    parser.add_argument("--stale", type=float, default=WORKER_STALE_SECONDS)
    parser.add_argument("--executor-delay", type=float, default=SYNTHETIC_DELAY_SECONDS)
    parser.add_argument("--executor-timeout", type=float, default=EXECUTOR_TIMEOUT_SECONDS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hoyo-browser-session")
    parser.add_argument("--version", action="store_true")
    commands = parser.add_subparsers(dest="command")

    doctor = commands.add_parser("doctor", help="inspect local Runtime and broker health")
    doctor.add_argument("--repo-root", type=Path, required=True)
    doctor.add_argument("--summary", action="store_true")

    inspect_jobs = commands.add_parser("inspect-jobs", help="show privacy-safe recent Job states")
    inspect_jobs.add_argument("--test-root", type=Path)
    inspect_jobs.add_argument("--external-root", type=Path, help=argparse.SUPPRESS)

    inspect_uncertain = commands.add_parser(
        "inspect-uncertain-job",
        help="show privacy-safe Jobs requiring publish outcome reconciliation",
    )
    inspect_uncertain.add_argument("--test-root", type=Path)
    inspect_uncertain.add_argument("--external-root", type=Path, help=argparse.SUPPRESS)

    acknowledge = commands.add_parser(
        "acknowledge-uncertain-job",
        help="clear one manually verified unknown publish outcome",
    )
    acknowledge.add_argument("--job-id", required=True)
    acknowledge.add_argument(
        "--confirmed-remote-checked",
        action="store_true",
        required=True,
    )
    acknowledge.add_argument("--test-root", type=Path, help=argparse.SUPPRESS)
    acknowledge.add_argument(
        "--external-root",
        type=Path,
        help=argparse.SUPPRESS,
    )

    inspect_session = commands.add_parser(
        "inspect-session",
        help="inspect Profile permissions, lock, process, and Singleton state",
    )
    inspect_session.add_argument("--repo-root", type=Path, required=True)
    inspect_session.add_argument("--test-root", type=Path)
    inspect_session.add_argument("--external-root", type=Path, help=argparse.SUPPRESS)

    launch = commands.add_parser(
        "browser-launch-check",
        help="launch and close Chromium with all page requests blocked",
    )
    launch.add_argument("--repo-root", type=Path, required=True)
    launch.add_argument("--test-root", type=Path)
    launch.add_argument("--external-root", type=Path, help=argparse.SUPPRESS)

    login = commands.add_parser(
        "login",
        help="establish exact multi-game Binding coverage in the shared Profile",
    )
    login.add_argument("--repo-root", type=Path, required=True)
    login.add_argument(
        "--account-scope",
        choices=(ACCOUNT_SCOPE,),
        required=True,
    )
    login.add_argument("--bootstrap-hsr-uid")

    owner = commands.add_parser("owner", help=argparse.SUPPRESS)
    owner.add_argument("--repo-root", type=Path, required=True)
    owner.add_argument("--test-root", type=Path)
    _add_test_timing_arguments(owner)

    commands.add_parser("owner-status", help="inspect the long-lived browser Owner")
    commands.add_parser("owner-shutdown", help="stop an idle browser Owner explicitly")
    recover_shutdown = commands.add_parser(
        "owner-recover-stale-shutdown",
        help="clear a stale shutdown marker after strict Owner absence checks",
    )
    recover_shutdown.add_argument(
        "--confirmed-owner-stopped",
        action="store_true",
        required=True,
    )
    recover_broken = commands.add_parser(
        "owner-recover-broken",
        help="clear a proven inactive BROKEN Owner record",
    )
    recover_broken.add_argument(
        "--confirmed-owner-stopped",
        action="store_true",
        required=True,
    )

    submit = commands.add_parser(
        "submit-synthetic",
        help="submit one fixed executor scenario in isolated test mode",
    )
    submit.add_argument("--test-root", type=Path, required=True)
    submit.add_argument("--repo-root", type=Path, required=True)
    submit.add_argument("--game", choices=("genshin", "zzz", "hsr"), required=True)
    submit.add_argument(
        "--scenario",
        choices=(
            "success",
            "failure",
            "rate-limit",
            "risk-control",
            "timeout",
        ),
        required=True,
    )
    submit.add_argument("--force", action="store_true")
    submit.add_argument("--timeout-seconds", type=float, default=30.0)
    _add_test_timing_arguments(submit)

    clear = commands.add_parser(
        "clear-test-block",
        help="explicitly clear an isolated synthetic risk block",
    )
    clear.add_argument("--test-root", type=Path, required=True)

    bootstrap = commands.add_parser("_bootstrap-init", help=argparse.SUPPRESS)
    bootstrap.add_argument("--repo-root", type=Path, required=True)
    bootstrap.add_argument("--external-root", type=Path, required=True)

    child = commands.add_parser("_synthetic-execute", help=argparse.SUPPRESS)
    child.add_argument("--test-root", type=Path, required=True)
    child.add_argument("--job-id", required=True)
    child.add_argument("--game", required=True)
    child.add_argument("--operation", required=True)
    child.add_argument("--request-interval", type=float, required=True)
    child.add_argument("--execution-delay", type=float, required=True)
    child.add_argument("--timeout-delay", type=float, required=True)
    return parser


async def _run_login(args: argparse.Namespace) -> int:
    repo_root = args.repo_root.expanduser().resolve(strict=True)
    settings = production_settings()
    if args.account_scope != settings.account_scope:
        raise ValueError("only the primary account scope is supported")
    initialize_external_state(settings, repo_root=repo_root)
    result = submit_and_wait(
        game=SESSION_GAME,
        operation=LOGIN_OPERATION,
        repo_root=repo_root,
        parameters=JobParameters(
            uid=args.bootstrap_hsr_uid,
            headed=True,
            request_interval=settings.shared_request_min_interval_seconds,
        ),
        force=True,
        timeout_seconds=settings.login_timeout_seconds + 30,
    )
    evidence = getattr(result, "evidence_codes", ())
    waiting = "LOGIN_WAITING_SIGNAL_EMITTED" in evidence
    if result.stable_code is StableCode.PASS:
        print("LOGIN_JOB_USES_OWNER_CONTEXT=YES")
        print("LOGIN_SECOND_CONTEXT_COUNT=0")
        print("LOGIN_SECOND_PROFILE_LOCK_COUNT=0")
        print("LOGIN_BROWSER_CONTEXT_COUNT=1")
        print("LOGIN_PROVIDER_COUNT=3")
        print("OWNER_CONTEXT_STAYS_OPEN_AFTER_LOGIN=YES")
        print(f"WAITING_SIGNAL_COUNT={1 if waiting else 0}")
        for game in ("GENSHIN", "ZZZ", "HSR"):
            prefix = f"{game}_BINDING_"
            status = next(
                (code.removeprefix(prefix) for code in evidence if code.startswith(prefix)),
                "UNREPORTED",
            )
            print(f"{game}_BINDING_COVERAGE={status}")
    print(f"RESULT_CODE={result.stable_code.value}")
    return 0 if result.stable_code is StableCode.PASS else 1


def _test_settings(args: argparse.Namespace) -> Settings:
    root = args.test_root
    if not isinstance(root, Path):
        raise ValueError("isolated test root is required")
    return isolated_test_settings(
        root,
        shared_request_min_interval_seconds=cast(
            float,
            getattr(args, "request_interval", SHARED_REQUEST_MIN_INTERVAL_SECONDS),
        ),
        post_job_cooldown_seconds=cast(
            float,
            getattr(args, "cooldown", POST_JOB_COOLDOWN_SECONDS),
        ),
        recent_success_reuse_seconds=cast(
            float,
            getattr(args, "reuse_window", RECENT_SUCCESS_REUSE_SECONDS),
        ),
        worker_heartbeat_seconds=cast(
            float,
            getattr(args, "heartbeat", WORKER_HEARTBEAT_SECONDS),
        ),
        worker_stale_seconds=cast(
            float,
            getattr(args, "stale", WORKER_STALE_SECONDS),
        ),
        executor_timeout_seconds=cast(
            float,
            getattr(args, "executor_timeout", EXECUTOR_TIMEOUT_SECONDS),
        ),
        synthetic_delay_seconds=cast(
            float,
            getattr(args, "executor_delay", SYNTHETIC_DELAY_SECONDS),
        ),
    )


def _settings_for_optional_test_root(args: argparse.Namespace) -> Settings:
    if getattr(args, "test_root", None) is not None:
        return _test_settings(args)
    external_root = getattr(args, "external_root", None)
    if isinstance(external_root, Path):
        return Settings(external_root=external_root)
    return production_settings()


def _print_job_result(result: object) -> None:
    from .models import JobResult

    if not isinstance(result, JobResult):
        raise TypeError("job result type was invalid")
    print(f"JOB_ID={result.job_id}")
    print(f"JOB_STATE={result.status.value}")
    print(f"JOB_DISPOSITION={result.disposition.value}")
    print(f"SUBMISSION_CODE={result.submission_code.value}")
    print(f"RESULT_CODE={result.stable_code.value}")
    print(f"REQUEST_COUNT={result.request_count}")
    if result.stable_code is StableCode.PUBLISH_OUTCOME_UNKNOWN:
        print("NO_OP=UNKNOWN")
        print("DATA_MODIFIED=UNKNOWN")
    else:
        print(f"NO_OP={'YES' if result.no_op else 'NO'}")
        print(f"DATA_MODIFIED={'YES' if result.data_modified else 'NO'}")


async def _run_async(args: argparse.Namespace) -> int:
    if args.command == "login":
        return await _run_login(args)
    if args.command == "owner":
        settings = _settings_for_optional_test_root(args)
        return await run_owner(settings, repo_root=args.repo_root)
    if args.command == "_synthetic-execute":
        settings = isolated_test_settings(
            args.test_root,
            shared_request_min_interval_seconds=args.request_interval,
        )
        return await run_synthetic_child(
            settings,
            job_id=args.job_id,
            game=args.game,
            operation=args.operation,
            execution_delay=args.execution_delay,
            timeout_delay=args.timeout_delay,
        )
    if args.command == "browser-launch-check":
        settings = _settings_for_optional_test_root(args)
        await run_owner_launch_check(settings, repo_root=args.repo_root)
        print("BROWSER_LAUNCH_CHECK=PASS")
        print("NETWORK_REQUEST_ALLOWED=NO")
        print("NETWORK_REQUEST_COUNT=0")
        print("POST_SHUTDOWN_PROFILE_PROCESS_COUNT=0")
        print("POST_SHUTDOWN_SINGLETON_COUNT=0")
        return 0
    if args.command == "doctor":
        settings = production_settings()
        summary = doctor_summary(settings, repo_root=args.repo_root)
        print("DOCTOR=PASS")
        print(f"REPOSITORY_IDENTITY={summary['repository_identity']}")
        print(f"RUNTIME_READINESS={summary['runtime_readiness']}")
        print(f"RUNTIME_PYTHON_READY={'YES' if summary['runtime_python_ready'] else 'NO'}")
        print(f"SQLITE_SCHEMA_VERSION={summary['sqlite_schema_version']}")
        print(f"SQLITE_JOURNAL_MODE={str(summary['sqlite_journal_mode']).upper()}")
        print(f"OWNER_STATE={summary['owner_state']}")
        print(f"OWNER_PHASE={summary['owner_phase']}")
        print(
            "OWNER_CONTEXT_GENERATION_PRESENT="
            f"{'YES' if summary['owner_context_generation_present'] else 'NO'}"
        )
        print(f"QUEUE_QUEUED={summary['queue_queued']}")
        print(f"QUEUE_RUNNING={summary['queue_running']}")
        print(f"ACCOUNT_STATE={summary['account_state']}")
        print(f"PROFILE_STATE={summary['profile_state']}")
        print(f"UNRESOLVED_UNCERTAIN_COUNT={summary['unresolved_uncertain_count']}")
        print(f"RETENTION_MAINTENANCE={summary['retention_maintenance_state']}")
        print(
            "RETENTION_DIAGNOSTIC_HASH="
            f"{summary['retention_maintenance_diagnostic_hash'] or 'NONE'}"
        )
        print(f"OPERATIONAL_EVENT_COUNT={summary['event_count']}")
        print(f"PROTOCOL_RESIDUE_COUNT={summary['protocol_residue_count']}")
        print(f"PROTOCOL_SAFE_JOB_COUNT={summary['protocol_safe_job_count']}")
        print(
            "PROTOCOL_UNSAFE_OR_UNCLASSIFIED_COUNT="
            f"{summary['protocol_unsafe_or_unclassified_count']}"
        )
        print(f"SQLITE_RESIDUE_COUNT={summary['sqlite_residue_count']}")
        print(f"EVENT_HISTORY_BOUNDED={'YES' if summary['event_history_bounded'] else 'NO'}")
        print(
            "TERMINAL_JOB_HISTORY_BOUNDED="
            f"{'YES' if summary['terminal_job_history_bounded'] else 'NO'}"
        )
        print(f"TERMINAL_HISTORY_TOTAL={summary['terminal_history_total']}")
        print(f"TERMINAL_HISTORY_LIMIT={summary['terminal_job_history_limit']}")
        print(f"TERMINAL_DELIVERY_GRACE_PROTECTED={summary['terminal_delivery_grace_protected']}")
        print(f"TERMINAL_PROTECTED_TOTAL={summary['terminal_protected_total']}")
        print(f"TERMINAL_PROTOCOL_PROTECTED={summary['terminal_protocol_protected']}")
        print(f"TERMINAL_ELIGIBLE_COUNT={summary['terminal_eligible_count']}")
        print(f"TERMINAL_PRUNABLE_EXCESS={summary['terminal_prunable_excess_count']}")
        print(f"DURABLE_PROTECTION_COUNT={summary['durable_protection_count']}")
        if not args.summary:
            recent_events = summary["recent_events"]
            if not isinstance(recent_events, list):
                raise TypeError("recent operational events were invalid")
            for event in recent_events:
                print(
                    "EVENT="
                    + json.dumps(
                        event,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
        return 0
    raise ValueError("command is not asynchronous")


def _run_sync(args: argparse.Namespace) -> int:
    if args.command == "owner-status":
        status = owner_status(production_settings())
        print(f"OWNER_STATE={status.state.value}")
        print(f"OWNER_PHASE={status.phase.value}")
        print(f"OWNER_RUNTIME_MATCH={'YES' if status.runtime_match else 'NO'}")
        print(f"OWNER_BROWSER_CONTEXT_READY={'YES' if status.browser_context_ready else 'NO'}")
        print(f"OWNER_WORKER_LOCK_HELD={'YES' if status.worker_lock_held else 'NO'}")
        print(f"OWNER_PROFILE_LOCK_HELD={'YES' if status.profile_lock_held else 'NO'}")
        print(f"OWNER_HEARTBEAT_FRESH={'YES' if status.heartbeat_fresh else 'NO'}")
        print(f"OWNER_PROCESS_ALIVE={'YES' if status.process_alive else 'NO'}")
        print(f"OWNER_QUEUE_DEPTH={status.queue_depth}")
        print(
            "OWNER_CONTEXT_GENERATION_PRESENT="
            f"{'YES' if status.context_generation_present else 'NO'}"
        )
        return 0
    if args.command == "owner-shutdown":
        settings = production_settings()
        try:
            request_owner_shutdown(settings)
        except OwnerQueueNotEmpty:
            print("SHUTDOWN_REQUEST=REJECTED_QUEUE_NOT_EMPTY")
            print("OWNER_CONTINUES_RUNNING=YES")
            return 1
        deadline = time.monotonic() + max(
            10.0,
            settings.worker_heartbeat_seconds * 4,
        )
        while time.monotonic() < deadline:
            if owner_status(settings).state.value == "STOPPED":
                break
            time.sleep(0.05)
        else:
            raise RuntimeNotReady("Owner did not stop after shutdown request")
        print("OWNER_SHUTDOWN=PASS")
        print("SESSION_RELOGIN_REQUIRED_ON_NEXT_START=YES")
        return 0
    if args.command == "owner-recover-stale-shutdown":
        recover_stale_shutdown(
            production_settings(),
            confirmed_owner_stopped=args.confirmed_owner_stopped,
        )
        print("OWNER_STALE_SHUTDOWN_RECOVERY=PASS")
        return 0
    if args.command == "owner-recover-broken":
        recover_broken_owner(
            production_settings(),
            confirmed_owner_stopped=args.confirmed_owner_stopped,
        )
        print("OWNER_BROKEN_RECOVERY=PASS")
        return 0
    if args.command == "inspect-jobs":
        settings = _settings_for_optional_test_root(args)
        jobs = JobQueue(settings).inspect_jobs()
        print(f"JOB_COUNT={len(jobs)}")
        for job in jobs:
            print(json.dumps(job, sort_keys=True, separators=(",", ":")))
        return 0
    if args.command == "inspect-uncertain-job":
        settings = _settings_for_optional_test_root(args)
        jobs = JobQueue(settings).inspect_uncertain_jobs()
        print(f"UNCERTAIN_JOB_COUNT={len(jobs)}")
        for job in jobs:
            print(json.dumps(job, sort_keys=True, separators=(",", ":")))
        return 0
    if args.command == "acknowledge-uncertain-job":
        settings = _settings_for_optional_test_root(args)
        code = JobQueue(settings).acknowledge_uncertain_job(
            args.job_id,
            confirmed_remote_checked=args.confirmed_remote_checked,
        )
        if code is StableCode.PUBLISH_OUTCOME_UNKNOWN_ACKNOWLEDGED:
            print("ACKNOWLEDGEMENT=RECORDED")
            print("RESULT_CODE=PUBLISH_OUTCOME_UNKNOWN_ACKNOWLEDGED")
            print("ACCOUNT_STATE=HEALTHY")
        else:
            print("ACKNOWLEDGEMENT=NOT_REQUIRED")
            print("LATE_RESULT_RECOVERY=APPLIED")
            print(f"RESULT_CODE={code.value}")
        return 0
    if args.command == "inspect-session":
        settings = _settings_for_optional_test_root(args)
        ProfileManager(settings).preflight(repo_root=args.repo_root)
        print("PROFILE_ID=primary")
        print("PROFILE_PREFLIGHT=PASS")
        return 0
    if args.command == "submit-synthetic":
        settings = _test_settings(args)
        operation = f"synthetic-{args.scenario}"
        result = submit_and_wait_with_settings(
            settings,
            game=args.game,
            operation=operation,
            repo_root=args.repo_root,
            runtime_python=Path(sys.executable),
            force=args.force,
            timeout_seconds=args.timeout_seconds,
        )
        _print_job_result(result)
        return 0 if result.stable_code is StableCode.PASS else 1
    if args.command == "clear-test-block":
        JobQueue(_test_settings(args)).clear_test_block()
        print("TEST_BLOCK_CLEAR=PASS")
        return 0
    if args.command == "_bootstrap-init":
        settings = Settings(external_root=args.external_root)
        initialize_external_state(settings, repo_root=args.repo_root)
        print("BOOTSTRAP_STATE_INIT=PASS")
        return 0
    raise ValueError("unknown command")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        from . import __version__

        print(__version__)
        return 0
    if args.command is None:
        parser.print_help()
        return 0
    redactor = Redactor(
        sensitive_values=(
            str(value)
            for value in (
                getattr(args, "repo_root", None),
                getattr(args, "test_root", None),
                getattr(args, "external_root", None),
            )
            if value is not None
        ),
        identity_values=(
            value for value in (getattr(args, "bootstrap_hsr_uid", None),) if isinstance(value, str)
        ),
    )
    try:
        if args.command in {
            "doctor",
            "login",
            "owner",
            "browser-launch-check",
            "_synthetic-execute",
        }:
            return asyncio.run(_run_async(args))
        return _run_sync(args)
    except KeyboardInterrupt:
        if args.command != "_synthetic-execute":
            print("ERROR_CODE=JOB_FAILED", file=sys.stderr)
        return 130
    except Exception as error:
        if args.command == "_synthetic-execute":
            return 1
        code, message = public_error(error)
        print(f"ERROR_CODE={code}", file=sys.stderr)
        print(f"ERROR={redactor.redact(message)}", file=sys.stderr)
        return 1
