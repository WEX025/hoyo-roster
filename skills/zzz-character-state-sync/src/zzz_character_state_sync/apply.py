"""ZZZ Apply policy over the shared transaction mechanism."""

from __future__ import annotations

import hashlib
import os  # noqa: F401 - compatibility for existing fault-injection callers
import shutil  # noqa: F401 - compatibility for existing fault-injection callers
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Self

from hoyo_browser_session.repository_mutation.apply import (
    ApplyAuthority,
    ApplyErrors,
    ApplyPolicy,
    ApplyTransactionState,
    TransactionInspection,
)
from hoyo_browser_session.repository_mutation.apply import (
    ApplyGuard as _SharedApplyGuard,
)
from hoyo_browser_session.repository_mutation.apply import (
    inspect_transaction as _inspect_transaction,
)
from hoyo_browser_session.repository_mutation.apply import (
    recover_transaction as _recover_transaction,
)
from hoyo_browser_session.repository_mutation.common import (
    MetadataError,
)
from hoyo_browser_session.repository_mutation.common import (
    tree_digest as _shared_tree_digest,
)

from .candidate import MUTATION_OWNER, ZZZ_RELATIVE, run_repository_validator
from .errors import (
    ApplyCleanupError,
    ApplyContractDirty,
    ApplyContractDrift,
    ApplyError,
    ApplyPostValidationError,
    ApplyRecoveryAmbiguous,
    ApplyRecoveryRequired,
    ApplyRecoveryResidue,
    CandidateValidationError,
)

DEFAULT_LOCK_PATH = Path("~/.local/share/hoyo-game-data/zzz-character-state-sync.lock")
CONTRACT_PATHS = (
    Path("schemas"),
    Path("tools/validate_repository.py"),
    Path("data/manifest.json"),
)
TRANSACTION_ROOT_RELATIVE = Path(".zzz-sync-transactions")
TRANSACTION_ROOT_MODE = 0o700
TRANSACTION_STATE_FILE = "transaction-state.json"
TRANSACTION_OWNER = MUTATION_OWNER


def _contract_digest(repo_root: Path) -> str:
    digest = hashlib.sha256()
    try:
        for relative in CONTRACT_PATHS:
            path = repo_root / relative
            digest.update(relative.as_posix().encode("utf-8"))
            digest.update(b"\0")
            if relative == Path("schemas"):
                digest.update(_shared_tree_digest(path).encode("ascii"))
            elif path.is_file() and not path.is_symlink():
                digest.update(path.read_bytes())
            else:
                raise MetadataError("repository contract path was unsafe")
            digest.update(b"\0")
    except (MetadataError, OSError, CandidateValidationError) as error:
        raise ApplyError("repository contract path is unsafe") from error
    return digest.hexdigest()


def _post_validation_error(error: BaseException) -> Exception:
    diagnostic: str | None = None
    if isinstance(error, CandidateValidationError):
        diagnostic = error.diagnostic_sha256
    if diagnostic is None:
        diagnostic = hashlib.sha256(type(error).__qualname__.encode("utf-8")).hexdigest()
    return ApplyPostValidationError(
        f"installed tree failed repository validation; diagnostic_sha256={diagnostic[:12]}"
    )


def _policy() -> ApplyPolicy:
    return ApplyPolicy(
        owner=TRANSACTION_OWNER,
        target_relative=ZZZ_RELATIVE,
        transaction_root_relative=TRANSACTION_ROOT_RELATIVE,
        default_lock_path=DEFAULT_LOCK_PATH,
        validator=lambda root: run_repository_validator(root),
        contract_digest=lambda root: _contract_digest(root),
        errors=ApplyErrors(
            apply=ApplyError,
            recovery_required=ApplyRecoveryRequired,
            residue=ApplyRecoveryResidue,
            cleanup=ApplyCleanupError,
            ambiguous=ApplyRecoveryAmbiguous,
            dirty=ApplyContractDirty,
            contract_drift=ApplyContractDrift,
            post_validation=_post_validation_error,
        ),
        legacy_format="zzz-v1",
        default_authority=ApplyAuthority.LOCAL_MAIN,
        prevalidate=False,
    )


class ApplyGuard:
    """Hold the shared external lock from preflight through candidate commit."""

    def __init__(
        self,
        repo_root: Path,
        *,
        expected_base_sha: str,
        authority: ApplyAuthority = ApplyAuthority.LOCAL_MAIN,
        lock_path: Path = DEFAULT_LOCK_PATH,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self._shared = _SharedApplyGuard(
            _policy(),
            repo_root,
            expected_base_sha=expected_base_sha,
            authority=authority,
            lock_path=lock_path,
            fault_injector=fault_injector,
        )
        self.repo_root = self._shared.repo_root
        self.target = self._shared.target
        self.transaction_root = self._shared.transaction_root

    @property
    def last_transaction_state(self) -> ApplyTransactionState | None:
        return self._shared.last_transaction_state

    def __enter__(self) -> Self:
        self._shared.__enter__()
        return self

    def assert_stable(self) -> None:
        self._shared.assert_stable()

    def commit(self, candidate_zzz: Path) -> None:
        self._shared.commit(candidate_zzz)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._shared.__exit__(exc_type, exc, traceback)


def inspect_transaction(repo_root: Path) -> TransactionInspection:
    return _inspect_transaction(_policy(), repo_root)


def recover_transaction(
    repo_root: Path,
    *,
    lock_path: Path = DEFAULT_LOCK_PATH,
    fault_injector: Callable[[str], None] | None = None,
) -> TransactionInspection:
    return _recover_transaction(
        _policy(),
        repo_root,
        lock_path=lock_path,
        fault_injector=fault_injector,
    )


__all__ = [
    "TRANSACTION_OWNER",
    "TRANSACTION_ROOT_MODE",
    "TRANSACTION_ROOT_RELATIVE",
    "TRANSACTION_STATE_FILE",
    "ApplyAuthority",
    "ApplyGuard",
    "ApplyTransactionState",
    "TransactionInspection",
    "inspect_transaction",
    "recover_transaction",
]
