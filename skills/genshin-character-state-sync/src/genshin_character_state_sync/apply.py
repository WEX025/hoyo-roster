"""Genshin Apply policy over the shared transaction mechanism."""

from __future__ import annotations

import fcntl  # noqa: F401 - compatibility for existing fault-injection callers
import hashlib
import os  # noqa: F401 - compatibility for existing fault-injection callers
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import IO, Self

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
    open_external_lock as _open_shared_lock,
)
from hoyo_browser_session.repository_mutation.apply import (
    recover_transaction as _recover_transaction,
)
from hoyo_browser_session.repository_mutation.common import (
    MetadataError,
    legacy_owner_v1_tree_digest,
)

from .candidate import (
    CONTRACT_PATHS as CANDIDATE_CONTRACT_PATHS,
)
from .candidate import (
    LEGACY_OWNER_V1,
    MUTATION_OWNER,
    Candidate,
    _inspection,
    canonical_business_digest,
    run_validator,
    tree_digest,
)
from .candidate import (
    contract_digest as candidate_contract_digest,
)
from .errors import ApplyError, ApplyRecoveryRequired, CandidateValidationError
from .lifecycle import GENSHIN_RELATIVE

DEFAULT_LOCK = Path("~/.local/share/hoyo-game-data/genshin-character-state-sync.lock")
TRANSACTION_ROOT = Path(".genshin-sync-transactions")
STATE_FILE = "transaction-state.json"
MODE = 0o700
TRANSACTION_OWNER = MUTATION_OWNER
LEGACY_TRANSACTION_OWNERS = LEGACY_OWNER_V1
CONTRACT_PATHS = (
    Path("schemas"),
    Path("tools/validate_repository.py"),
    Path("data/manifest.json"),
)


def _contract_digest(repo_root: Path) -> str:
    digest = hashlib.sha256()
    try:
        for relative in CONTRACT_PATHS:
            path = repo_root / relative
            digest.update(relative.as_posix().encode("utf-8"))
            if path.is_dir() and not path.is_symlink():
                digest.update(legacy_owner_v1_tree_digest(path).encode("ascii"))
            elif path.is_file() and not path.is_symlink():
                digest.update(path.read_bytes())
            else:
                raise MetadataError("repository contract was unsafe")
    except (MetadataError, OSError) as error:
        raise ApplyError("repository contract was unsafe") from error
    return digest.hexdigest()


def _policy() -> ApplyPolicy:
    return ApplyPolicy(
        owner=TRANSACTION_OWNER,
        target_relative=GENSHIN_RELATIVE,
        transaction_root_relative=TRANSACTION_ROOT,
        default_lock_path=DEFAULT_LOCK,
        validator=lambda root: run_validator(root),
        contract_digest=lambda root: _contract_digest(root),
        errors=ApplyErrors(
            apply=ApplyError,
            recovery_required=ApplyRecoveryRequired,
            residue=ApplyRecoveryRequired,
            cleanup=ApplyError,
            ambiguous=ApplyRecoveryRequired,
            dirty=ApplyError,
            contract_drift=ApplyError,
        ),
        legacy_format="owner-v1",
        legacy_owners=LEGACY_TRANSACTION_OWNERS,
        legacy_tree_digest=legacy_owner_v1_tree_digest,
        default_authority=ApplyAuthority.HEAD_ONLY,
    )


def _assert_candidate_contract(candidate: Candidate, repo_root: Path) -> None:
    try:
        if candidate_contract_digest(candidate.root) != candidate_contract_digest(repo_root):
            raise ApplyError("candidate contract drifted")
        for relative in CANDIDATE_CONTRACT_PATHS:
            if (candidate.root / relative).read_bytes() != (repo_root / relative).read_bytes():
                raise ApplyError("candidate contract drifted")
    except OSError as error:
        raise ApplyError("candidate contract was unsafe") from error


def _open_external_lock(lock_path: Path, repo_root: Path) -> IO[str]:
    return _open_shared_lock(_policy(), lock_path, repo_root)


class ApplyGuard:
    def __init__(
        self,
        repo_root: Path,
        *,
        expected_base_sha: str,
        lock_path: Path = DEFAULT_LOCK,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self._shared = _SharedApplyGuard(
            _policy(),
            repo_root,
            expected_base_sha=expected_base_sha,
            lock_path=lock_path,
            fault_injector=fault_injector,
        )

    @property
    def last_transaction_state(self) -> ApplyTransactionState | None:
        return self._shared.last_transaction_state

    def __enter__(self) -> Self:
        self._shared.__enter__()
        return self

    def assert_stable(self) -> None:
        self._shared.assert_stable()

    def commit(self, candidate: Candidate) -> None:
        inspection = _inspection(candidate.root)
        if (
            not inspection.cleanup_allowed
            or inspection.status != "VALIDATED"
            or inspection.run_id != candidate.root.name
            or inspection.game_tree_digest is None
            or inspection.business_digest is None
            or inspection.contract_digest is None
        ):
            raise ApplyError("candidate was not validated")
        if inspection.contract_digest != candidate_contract_digest(self._shared.repo_root):
            raise ApplyError("candidate contract digest did not match repository")
        _assert_candidate_contract(candidate, self._shared.repo_root)
        try:
            run_validator(candidate.root, scope_game="genshin-impact")
            if _inspection(candidate.root) != inspection:
                raise ApplyError("candidate validation state drifted")
            if tree_digest(candidate.game_dir) != inspection.game_tree_digest:
                raise ApplyError("candidate tree drifted after validation")
            if canonical_business_digest(candidate.root) != inspection.business_digest:
                raise ApplyError("candidate business data drifted after validation")
            _assert_candidate_contract(candidate, self._shared.repo_root)
        except ApplyError:
            raise
        except (CandidateValidationError, OSError) as error:
            raise ApplyError("candidate revalidation failed") from error
        self._shared.commit(
            candidate.game_dir,
            expected_source_digest=inspection.game_tree_digest,
        )

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
    lock_path: Path = DEFAULT_LOCK,
    fault_injector: Callable[[str], None] | None = None,
) -> TransactionInspection:
    return _recover_transaction(
        _policy(),
        repo_root,
        lock_path=lock_path,
        fault_injector=fault_injector,
    )
