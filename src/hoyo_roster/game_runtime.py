"""Game result types and injected per-role execution; no game implementation imports."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path

from .validation import CONTRACT_ROOT
from .models import OwnedRole
from .errors import (
    OperationError,
    WrongAccount,
    LoginBindingFailed,
    LoginNetworkError,
    LoginSessionInitializationFailed,
)
from .progress import emit
from .sync import OperationResult
from .user_workspace import LocalSnapshotStore, WorkspaceError, direct_configuration

ACCOUNT_STOP_CODES = frozenset(
    {
        "WRONG_ACCOUNT",
        "ACCOUNT_IDENTITY_MISMATCH",
        "IDENTITY_MISMATCH",
        "EXPECTED_UID_NOT_FOUND",
        "EXPECTED_UID_AMBIGUOUS",
        "RATE_LIMITED",
        "RISK_CONTROL",
        "RISK_CONTROL_ERROR",
        "AUTHENTICATION_REQUIRED",
        "BROWSER_CONTEXT_UNAVAILABLE",
        "BROWSER_CLOSE_FAILED",
        "BROWSER_UNAVAILABLE",
        "BROWSER_PROFILE_INVALID",
        "USER_WORKSPACE_BUSY",
        "REQUEST_SCHEDULE_INVALID",
    }
)


def stops_account(error, code):
    # A target game's service/Binding contract failure does not invalidate the
    # shared browser. Other games still perform their own official session check.
    game_service_failure = isinstance(
        error, (LoginBindingFailed, LoginNetworkError, LoginSessionInitializationFailed)
    )
    return code in ACCOUNT_STOP_CODES or (
        isinstance(error, OperationError) and not game_service_failure
    )


@dataclass(frozen=True, slots=True, repr=False)
class RoleResult:
    role: OwnedRole
    status: str
    error_code: str | None = None
    result_scope: Path | None = None
    current_pointer: Path | None = None
    current_result: Path | None = None
    commit_state: str = "NOT_COMMITTED"
    cleanup_pending: bool = False
    operation: OperationResult | None = None


@dataclass(frozen=True, slots=True)
class GameResult:
    game: str
    status: str
    roles: tuple[RoleResult, ...] = field(default_factory=tuple)
    error_code: str | None = None
    stop_account: bool = False

    @property
    def failed(self):
        return self.status == "FAIL" or any(role.status == "FAIL" for role in self.roles)

    def report(self, *, finalized=True):
        status = self.status if finalized or self.status != "PASS" else "RESULT_AVAILABLE"
        print(f"GAME={self.game}\nGAME_STATUS={status}", flush=True)
        if self.error_code:
            print(f"ERROR_CODE={self.error_code}", flush=True)
        if self.status == "NO_ROLES":
            print("COLLECTION_STATUS=NOT_COLLECTED", flush=True)
        for role in self.roles:
            print(f"ROLE_STATUS={role.status}", flush=True)
            if role.error_code:
                print(f"ERROR_CODE={role.error_code}", flush=True)
            for name, value in (
                ("RESULT_SCOPE", role.result_scope),
                ("CURRENT_POINTER", role.current_pointer),
                ("CURRENT_RESULT", role.current_result),
            ):
                if value is not None:
                    print(f"{name}={value}", flush=True)
            print(
                f"COMMIT_STATE={role.commit_state}\nCLEANUP_PENDING={'YES' if role.cleanup_pending else 'NO'}"
            )
            if role.cleanup_pending:
                emit("CLEANUP_PENDING")
            if role.operation is not None and finalized:
                role.operation.report()


async def execute_roles(args, auth, roles, *, game, candidate_policy, sync, sync_error):
    """Called by a game's public module; only injected game policies execute here."""
    if args.uid is not None:
        roles = tuple(role for role in roles if role.uid == args.uid)
        if len(roles) != 1:
            raise WrongAccount("Requested role was not bound to the current official account")
    if not roles:
        return GameResult(game, "NO_ROLES")
    if args.operation in {"discover", "login"}:
        return GameResult(game, "PASS", tuple(RoleResult(role, "VERIFIED") for role in roles))
    results = []
    for role in roles:
        config = direct_configuration(game, role.uid, storage_root=args.storage_root)
        store = None
        try:
            store = LocalSnapshotStore(
                CONTRACT_ROOT, config, game, candidate_policy=candidate_policy()
            )
            with store.locked():
                workspace = store.prepare()
                if store.cleanup_pending:
                    raise WorkspaceError("RECOVERY_REQUIRED")
                domain = argparse.Namespace(
                    repo_root=workspace,
                    uid=role.uid,
                    expected_region=role.region,
                    headed=auth.headed,
                    request_interval=2.0,
                )
                operation = await sync(
                    domain, snapshot_store=store, request_gate_override=auth.gate, auth_service=auth
                )
                current = store.current()
                result = RoleResult(
                    role,
                    "UNCHANGED" if operation.no_op else "SAVED",
                    result_scope=store.root,
                    current_pointer=store.current_file,
                    current_result=current / "data" / store.game if current else None,
                    commit_state=store.commit_state,
                    cleanup_pending=store.cleanup_pending,
                    operation=operation,
                )
        except Exception as error:
            code = (
                str(error)
                if isinstance(error, WorkspaceError)
                else error.code
                if isinstance(error, (sync_error, OperationError))
                else "LOCAL_OPERATION_FAILED"
            )
            result = RoleResult(
                role,
                "FAIL",
                code,
                result_scope=store.root if store else None,
                current_pointer=store.current_file if store else None,
                commit_state=store.commit_state if store else "NOT_COMMITTED",
                cleanup_pending=store.cleanup_pending if store else False,
            )
            results.append(result)
            if stops_account(error, code):
                return GameResult(game, "FAIL", tuple(results), code, True)
            continue
        results.append(result)
    return GameResult(
        game, "FAIL" if any(r.status == "FAIL" for r in results) else "PASS", tuple(results)
    )
