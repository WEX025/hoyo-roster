"""Fact-based atomic replacement and deterministic transaction recovery."""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import IO, Self

from hoyo_browser_session.repository_identity import (
    GitExecutionError,
    git_output,
    git_succeeds,
)

from .common import (
    MetadataError,
    atomic_write_metadata,
    is_atomic_temporary_name,
    load_metadata,
    raise_as,
    require_real_directory,
    tree_digest,
)

TRANSACTION_AUTHORITY = "hoyo-repository-mutation/apply-v2"
TRANSACTION_STATE_VERSION = 2
TRANSACTION_STATE_FILE = "transaction-state.json"
TRANSACTION_ROOT_MODE = 0o700
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

PHASES = {
    "STARTED",
    "STAGED",
    "BACKED_UP",
    "INSTALLED",
    "VALIDATED",
    "RECOVERING_OLD",
    "OLD_RESTORED",
    "RECOVERING_NEW",
    "NEW_COMMITTED",
}
PHASE_ORDER = {
    "STARTED": 0,
    "STAGED": 1,
    "BACKED_UP": 2,
    "INSTALLED": 3,
    "VALIDATED": 4,
    "RECOVERING_OLD": 5,
    "RECOVERING_NEW": 5,
    "OLD_RESTORED": 6,
    "NEW_COMMITTED": 6,
}

FaultInjector = Callable[[str], None]
LegacyApplyFormat = str


class ApplyAuthority(Enum):
    """Fixed Git authorities accepted by the shared Apply guard."""

    HEAD_ONLY = "head-only"
    LOCAL_MAIN = "refs/heads/main"
    ORIGIN_MAIN = "refs/remotes/origin/main"


@dataclass(frozen=True, slots=True)
class ApplyErrors:
    apply: type[Exception]
    recovery_required: type[Exception]
    residue: type[Exception]
    cleanup: type[Exception]
    ambiguous: type[Exception]
    dirty: type[Exception]
    contract_drift: type[Exception]
    post_validation: Callable[[BaseException], Exception] | None = None


@dataclass(frozen=True, slots=True)
class ApplyPolicy:
    owner: str
    target_relative: Path
    transaction_root_relative: Path
    default_lock_path: Path
    validator: Callable[[Path], None]
    contract_digest: Callable[[Path], str]
    errors: ApplyErrors
    legacy_format: LegacyApplyFormat
    legacy_owners: tuple[str, ...] = ()
    legacy_tree_digest: Callable[[Path], str] | None = None
    default_authority: ApplyAuthority = ApplyAuthority.HEAD_ONLY
    prevalidate: bool = True

    def __post_init__(self) -> None:
        for path in (self.target_relative, self.transaction_root_relative):
            if path.is_absolute() or not path.parts or ".." in path.parts:
                raise ValueError("apply policy path was invalid")
        if not self.owner or self.legacy_format not in {"owner-v1", "zzz-v1"}:
            raise ValueError("apply policy was invalid")


@dataclass(frozen=True, slots=True)
class TransactionInspection:
    status: str
    transaction_id: str | None
    validator_pass: bool
    metadata_version: int | None = None


@dataclass(slots=True)
class ApplyTransactionState:
    staging_created: bool = False
    old_tree_moved_to_backup: bool = False
    new_tree_installed: bool = False
    new_tree_validated: bool = False
    old_tree_restored: bool = False
    backup_cleanup_complete: bool = False
    recovery_required: bool = False


@dataclass(slots=True)
class _TransactionRecord:
    document: dict[str, object]
    transaction_id: str
    phase: str
    old_digest: str
    new_digest: str
    contract_digest: str
    backup: Path
    staging: Path
    metadata_version: int
    legacy_format: LegacyApplyFormat | None


def _fault(injector: FaultInjector | None, stage: str) -> None:
    if injector is not None:
        injector(stage)


def _git(policy: ApplyPolicy, repo_root: Path, *arguments: str) -> str:
    try:
        return git_output(repo_root, *arguments)
    except GitExecutionError as error:
        raise_as(policy.errors.apply, "git preflight failed", error)


def _git_succeeds(policy: ApplyPolicy, repo_root: Path, *arguments: str) -> bool:
    try:
        return git_succeeds(repo_root, *arguments)
    except GitExecutionError as error:
        raise_as(policy.errors.apply, "git preflight failed", error)


def _contract_digest(policy: ApplyPolicy, repo_root: Path) -> str:
    try:
        value = policy.contract_digest(repo_root)
    except BaseException as error:
        if not isinstance(error, Exception):
            raise
        raise_as(policy.errors.apply, "repository contract was unsafe", error)
    if type(value) is not str or SHA256_RE.fullmatch(value) is None:
        raise_as(policy.errors.apply, "repository contract digest was invalid")
    return value


def _tree_digest(policy: ApplyPolicy, path: Path) -> str:
    try:
        return tree_digest(path)
    except (MetadataError, OSError) as error:
        raise_as(policy.errors.apply, "repository tree was unsafe", error)


def _validator_passes(policy: ApplyPolicy, repo_root: Path) -> bool:
    try:
        policy.validator(repo_root)
    except Exception:
        return False
    return True


def _ensure_real_directory_chain(policy: ApplyPolicy, path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not os.path.lexists(current):
        missing.append(current)
        current = current.parent
    try:
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise MetadataError("lock parent was unsafe")
        for item in reversed(missing):
            item.mkdir(mode=TRANSACTION_ROOT_MODE)
            require_real_directory(item)
    except (MetadataError, OSError) as error:
        raise_as(policy.errors.apply, "external lock parent was unsafe", error)


def open_external_lock(
    policy: ApplyPolicy,
    lock_path: Path,
    repo_root: Path,
) -> IO[str]:
    path = Path(os.path.abspath(lock_path.expanduser()))
    repository = repo_root.resolve()
    if path == repository or path.is_relative_to(repository):
        raise_as(policy.errors.apply, "external lock must be outside the repository")
    descriptor: int | None = None
    try:
        _ensure_real_directory_chain(policy, path.parent)
        resolved_parent = path.parent.resolve(strict=True)
        if resolved_parent == repository or resolved_parent.is_relative_to(repository):
            raise_as(
                policy.errors.apply,
                "external lock must be outside the repository",
            )
        existing: os.stat_result | None = None
        if os.path.lexists(path):
            existing = path.lstat()
            if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
                raise MetadataError("lock path was unsafe")
        flags = os.O_APPEND | os.O_CREAT | os.O_NONBLOCK | os.O_RDWR
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            existing is not None
            and (existing.st_dev, existing.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise MetadataError("lock identity changed")
        stream = os.fdopen(descriptor, "a+", encoding="utf-8")
        descriptor = None
        return stream
    except Exception as error:
        if isinstance(error, policy.errors.apply):
            raise
        raise_as(policy.errors.apply, "external lock was unsafe", error)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _acquire_lock(policy: ApplyPolicy, stream: IO[str]) -> None:
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise_as(
            policy.errors.apply,
            "another synchronization holds the external lock",
        )
    except OSError as error:
        raise_as(policy.errors.apply, "external lock could not be acquired", error)


def _attached_branch(policy: ApplyPolicy, repo_root: Path) -> str:
    branch = _git(policy, repo_root, "symbolic-ref", "--quiet", "--short", "HEAD")
    if not branch:
        raise_as(policy.errors.apply, "apply is forbidden on detached HEAD")
    if branch == "main":
        raise_as(policy.errors.apply, "apply is forbidden on main")
    expected_ref = f"refs/heads/{branch}"
    listing = _git(policy, repo_root, "worktree", "list", "--porcelain")
    current_path: Path | None = None
    current_branch: str | None = None
    matches = 0
    for line in (*listing.splitlines(), ""):
        if line.startswith("worktree "):
            current_path = Path(line.removeprefix("worktree ")).resolve()
            current_branch = None
        elif line.startswith("branch "):
            current_branch = line.removeprefix("branch ")
        elif not line:
            if current_path == repo_root and current_branch == expected_ref:
                matches += 1
            current_path = None
            current_branch = None
    if matches != 1:
        raise_as(policy.errors.apply, "apply requires an attached branch worktree")
    return branch


def _worktree_changes(policy: ApplyPolicy, repo_root: Path) -> list[str]:
    lines = _git(
        policy,
        repo_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    ).splitlines()
    prefix = f"?? {policy.transaction_root_relative.as_posix()}/"
    return [line for line in lines if not line.startswith(prefix)]


def _assert_git_guard(
    policy: ApplyPolicy,
    repo_root: Path,
    *,
    expected_base_sha: str,
    authority: ApplyAuthority,
    check_clean: bool = True,
) -> tuple[str, str]:
    if not isinstance(authority, ApplyAuthority):
        raise_as(policy.errors.apply, "apply authority must be a fixed internal value")
    if SHA_RE.fullmatch(expected_base_sha) is None:
        raise_as(
            policy.errors.apply,
            "expected Base SHA must be a 40-character lowercase SHA",
        )
    branch = _attached_branch(policy, repo_root)
    expected = _git(
        policy,
        repo_root,
        "rev-parse",
        "--verify",
        f"{expected_base_sha}^{{commit}}",
    )
    if expected != expected_base_sha:
        raise_as(policy.errors.apply, "expected Base SHA did not resolve exactly")
    head = _git(policy, repo_root, "rev-parse", "HEAD")
    if head != expected_base_sha:
        raise_as(policy.errors.apply, "HEAD does not equal expected Base SHA")
    if authority is not ApplyAuthority.HEAD_ONLY:
        authority_ref = authority.value
        if not _git_succeeds(
            policy,
            repo_root,
            "show-ref",
            "--verify",
            "--quiet",
            authority_ref,
        ):
            raise_as(policy.errors.apply, "authoritative Base ref was unavailable")
        if authority is ApplyAuthority.LOCAL_MAIN:
            if not _git_succeeds(
                policy,
                repo_root,
                "merge-base",
                "--is-ancestor",
                expected_base_sha,
                authority_ref,
            ):
                raise_as(
                    policy.errors.apply,
                    "expected Base SHA is not an ancestor of local main",
                )
        else:
            authority_sha = _git(
                policy,
                repo_root,
                "rev-parse",
                "--verify",
                f"{authority_ref}^{{commit}}",
            )
            if authority_sha != expected_base_sha:
                raise_as(
                    policy.errors.apply,
                    "authoritative origin/main does not equal expected Base SHA",
                )
    if check_clean and _worktree_changes(policy, repo_root):
        raise_as(
            policy.errors.dirty,
            "entire Git worktree must be clean for apply",
        )
    return branch, head


def _transaction_root(policy: ApplyPolicy, repo_root: Path) -> Path:
    return repo_root / policy.transaction_root_relative


def _state_entries(root: Path) -> list[os.DirEntry[str]]:
    entries: list[os.DirEntry[str]] = []
    for entry in os.scandir(root):
        if entry.name == TRANSACTION_STATE_FILE or is_atomic_temporary_name(
            entry.name,
            TRANSACTION_STATE_FILE,
        ):
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise MetadataError("transaction state entry was unsafe")
            entries.append(entry)
    return entries


def _canonical_uuid(value: object) -> str:
    if type(value) is not str:
        raise ValueError
    canonical = str(uuid.UUID(value))
    if canonical != value:
        raise ValueError
    return canonical


def _digest(value: object) -> str:
    if type(value) is not str or SHA256_RE.fullmatch(value) is None:
        raise ValueError
    return value


def _v2_record(
    policy: ApplyPolicy,
    repo_root: Path,
    document: dict[str, object],
) -> _TransactionRecord:
    required = {
        "authority",
        "version",
        "owner",
        "transaction_id",
        "phase",
        "target",
        "old_digest",
        "new_digest",
        "contract_digest",
        "backup",
        "staging",
        "created_at",
    }
    transaction_id = _canonical_uuid(document.get("transaction_id"))
    phase = document.get("phase")
    expected_backup = f"backup-{transaction_id}"
    expected_staging = f"staging-{transaction_id}"
    if (
        set(document) != required
        or document.get("authority") != TRANSACTION_AUTHORITY
        or type(document.get("version")) is not int
        or document["version"] != TRANSACTION_STATE_VERSION
        or document.get("owner") != policy.owner
        or phase not in PHASES
        or document.get("target") != policy.target_relative.as_posix()
        or document.get("backup") != expected_backup
        or document.get("staging") != expected_staging
        or type(document.get("created_at")) is not str
    ):
        raise ValueError
    datetime.fromisoformat(str(document["created_at"]))
    root = _transaction_root(policy, repo_root)
    return _TransactionRecord(
        document,
        transaction_id,
        str(phase),
        _digest(document["old_digest"]),
        _digest(document["new_digest"]),
        _digest(document["contract_digest"]),
        root / expected_backup,
        root / expected_staging,
        TRANSACTION_STATE_VERSION,
        None,
    )


def _owner_v1_record(
    policy: ApplyPolicy,
    repo_root: Path,
    document: dict[str, object],
) -> _TransactionRecord:
    required = {
        "version",
        "owner",
        "transaction_id",
        "phase",
        "old_digest",
        "new_digest",
        "contract_digest",
    }
    transaction_id = _canonical_uuid(document.get("transaction_id"))
    phase = document.get("phase")
    phase_map = {
        "started": "STARTED",
        "staged": "STAGED",
        "backed_up": "BACKED_UP",
        "installed": "INSTALLED",
        "validated": "VALIDATED",
    }
    if (
        set(document) != required
        or type(document.get("version")) is not int
        or document["version"] != 1
        or document.get("owner") not in policy.legacy_owners
        or phase not in phase_map
    ):
        raise ValueError
    root = _transaction_root(policy, repo_root)
    return _TransactionRecord(
        document,
        transaction_id,
        phase_map[str(phase)],
        _digest(document["old_digest"]),
        _digest(document["new_digest"]),
        _digest(document["contract_digest"]),
        root / f"backup-{transaction_id}",
        root / f"staging-{transaction_id}",
        1,
        "owner-v1",
    )


def _zzz_v1_record(
    policy: ApplyPolicy,
    repo_root: Path,
    document: dict[str, object],
) -> _TransactionRecord:
    required = {
        "version",
        "transaction_id",
        "phase",
        "expected_target",
        "old_tree_digest",
        "new_tree_digest",
        "backup_path",
        "staging_path",
        "created_at",
        "contract_digest",
    }
    transaction_id = _canonical_uuid(document.get("transaction_id"))
    phase = document.get("phase")
    expected_backup = (policy.transaction_root_relative / f"backup-{transaction_id}").as_posix()
    expected_staging = (policy.transaction_root_relative / f"staging-{transaction_id}").as_posix()
    if (
        set(document) != required
        or type(document.get("version")) is not int
        or document["version"] != 1
        or phase not in PHASES
        or document.get("expected_target") != policy.target_relative.as_posix()
        or document.get("backup_path") != expected_backup
        or document.get("staging_path") != expected_staging
        or type(document.get("created_at")) is not str
    ):
        raise ValueError
    datetime.fromisoformat(str(document["created_at"]))
    root = _transaction_root(policy, repo_root)
    return _TransactionRecord(
        document,
        transaction_id,
        str(phase),
        _digest(document["old_tree_digest"]),
        _digest(document["new_tree_digest"]),
        _digest(document["contract_digest"]),
        root / f"backup-{transaction_id}",
        root / f"staging-{transaction_id}",
        1,
        "zzz-v1",
    )


def _parse_document(
    policy: ApplyPolicy,
    repo_root: Path,
    document: object,
) -> _TransactionRecord:
    if not isinstance(document, dict):
        raise ValueError
    value = dict(document)
    if value.get("authority") == TRANSACTION_AUTHORITY:
        return _v2_record(policy, repo_root, value)
    if policy.legacy_format == "owner-v1":
        return _owner_v1_record(policy, repo_root, value)
    return _zzz_v1_record(policy, repo_root, value)


def _record_identity(record: _TransactionRecord) -> tuple[object, ...]:
    return (
        record.transaction_id,
        record.old_digest,
        record.new_digest,
        record.contract_digest,
        record.backup.name,
        record.staging.name,
        record.metadata_version,
        record.legacy_format,
    )


def _read_record(policy: ApplyPolicy, repo_root: Path) -> _TransactionRecord:
    root = _transaction_root(policy, repo_root)
    metadata = require_real_directory(root)
    if stat.S_IMODE(metadata.st_mode) != TRANSACTION_ROOT_MODE:
        raise MetadataError("transaction root mode was invalid")
    state_entries = _state_entries(root)
    if not state_entries:
        raise MetadataError("transaction state was missing")
    records: list[_TransactionRecord] = []
    formal_valid = False
    for entry in state_entries:
        try:
            record = _parse_document(
                policy,
                repo_root,
                load_metadata(Path(entry.path)),
            )
        except (MetadataError, OSError, ValueError):
            if entry.name == TRANSACTION_STATE_FILE:
                raise MetadataError("formal transaction state was invalid") from None
            continue
        records.append(record)
        formal_valid = formal_valid or entry.name == TRANSACTION_STATE_FILE
    if not records:
        raise MetadataError("transaction state was unreadable")
    if not formal_valid and len(records) != len(state_entries):
        raise MetadataError("temporary transaction state was incomplete")
    identity = _record_identity(records[0])
    if any(_record_identity(record) != identity for record in records[1:]):
        raise MetadataError("transaction state versions disagreed")
    highest_order = max(PHASE_ORDER[record.phase] for record in records)
    highest = [record for record in records if PHASE_ORDER[record.phase] == highest_order]
    if len({record.phase for record in highest}) != 1:
        raise MetadataError("transaction recovery direction was ambiguous")
    record = highest[0]
    allowed = {
        TRANSACTION_STATE_FILE,
        record.backup.name,
        record.staging.name,
        *(entry.name for entry in state_entries),
    }
    if any(entry.name not in allowed for entry in os.scandir(root)):
        raise MetadataError("transaction root contained unowned entries")
    return record


def _existing_digest(policy: ApplyPolicy, path: Path) -> str | None:
    if not os.path.lexists(path):
        return None
    return _tree_digest(policy, path)


def _record_digest(
    policy: ApplyPolicy,
    record: _TransactionRecord,
    path: Path,
) -> str | None:
    if not os.path.lexists(path):
        return None
    if record.legacy_format == "owner-v1" and policy.legacy_tree_digest is not None:
        try:
            return policy.legacy_tree_digest(path)
        except Exception as error:
            raise_as(policy.errors.apply, "legacy transaction tree was unsafe", error)
    return _tree_digest(policy, path)


def _classify(
    record: _TransactionRecord,
    *,
    target_digest: str | None,
    backup_digest: str | None,
    staging_digest: str | None,
    validator_pass: bool,
) -> str:
    phase = record.phase
    old_digest = record.old_digest
    new_digest = record.new_digest
    if (
        target_digest == old_digest
        and backup_digest is None
        and staging_digest in {None, new_digest}
        and phase in {"STARTED", "STAGED"}
        and validator_pass
    ):
        return "PRE_INSTALL_RESIDUE"
    if (
        target_digest is None
        and backup_digest == old_digest
        and staging_digest in {None, new_digest}
        and phase
        in {
            "STAGED",
            "BACKED_UP",
            "INSTALLED",
            "VALIDATED",
            "RECOVERING_OLD",
            "OLD_RESTORED",
        }
    ):
        return "OLD_TREE_IN_BACKUP"
    if (
        target_digest == new_digest
        and backup_digest == old_digest
        and staging_digest is None
        and phase
        in {
            "STAGED",
            "BACKED_UP",
            "INSTALLED",
            "VALIDATED",
            "RECOVERING_OLD",
        }
        and not (phase == "VALIDATED" and validator_pass)
    ):
        return "NEW_TREE_INSTALLED_UNVALIDATED"
    if (
        target_digest == new_digest
        and backup_digest == old_digest
        and staging_digest is None
        and phase in {"VALIDATED", "RECOVERING_NEW", "NEW_COMMITTED"}
        and validator_pass
    ):
        return "NEW_TREE_VALIDATED_BACKUP_RETAINED"
    if (
        target_digest == old_digest
        and backup_digest is None
        and staging_digest in {None, new_digest}
        and phase
        in {
            "BACKED_UP",
            "INSTALLED",
            "VALIDATED",
            "RECOVERING_OLD",
            "OLD_RESTORED",
        }
        and validator_pass
    ):
        return "OLD_TREE_RESTORED_CLEANUP_PENDING"
    if (
        target_digest == new_digest
        and backup_digest is None
        and staging_digest is None
        and phase in {"VALIDATED", "RECOVERING_NEW", "NEW_COMMITTED"}
        and validator_pass
    ):
        return "NEW_TREE_COMMITTED_CLEANUP_PENDING"
    return "AMBIGUOUS_RECOVERY_REQUIRED"


def inspect_transaction(
    policy: ApplyPolicy,
    repo_root: Path,
) -> TransactionInspection:
    repository = repo_root.resolve()
    root = _transaction_root(policy, repository)
    validator_pass = _validator_passes(policy, repository)
    if not os.path.lexists(root):
        return TransactionInspection("CLEAN", None, validator_pass)
    try:
        metadata = require_real_directory(root)
        if stat.S_IMODE(metadata.st_mode) != TRANSACTION_ROOT_MODE:
            raise MetadataError("transaction root mode was invalid")
        if next(os.scandir(root), None) is None:
            return TransactionInspection(
                "EMPTY_TRANSACTION_ROOT_CLEANUP_PENDING",
                None,
                validator_pass,
            )
        record = _read_record(policy, repository)
        if _contract_digest(policy, repository) != record.contract_digest:
            raise MetadataError("transaction contract drifted")
        target = repository / policy.target_relative
        status = _classify(
            record,
            target_digest=_record_digest(policy, record, target),
            backup_digest=_record_digest(policy, record, record.backup),
            staging_digest=_record_digest(policy, record, record.staging),
            validator_pass=validator_pass,
        )
        return TransactionInspection(
            status,
            record.transaction_id,
            validator_pass,
            record.metadata_version,
        )
    except Exception:
        return TransactionInspection(
            "AMBIGUOUS_RECOVERY_REQUIRED",
            None,
            False,
        )


def _write_phase(
    policy: ApplyPolicy,
    repo_root: Path,
    record: _TransactionRecord,
    phase: str,
    fault_injector: FaultInjector | None,
) -> None:
    if phase not in PHASES:
        raise ValueError("transaction phase was invalid")
    if record.legacy_format == "owner-v1":
        return
    record.phase = phase
    record.document["phase"] = phase
    atomic_write_metadata(
        _transaction_root(policy, repo_root) / TRANSACTION_STATE_FILE,
        record.document,
        before_replace=lambda temporary: _fault(
            fault_injector,
            f"during_state_write_{phase.lower()}",
        ),
    )


def _remove_transaction_tree(
    policy: ApplyPolicy,
    path: Path,
) -> None:
    if not os.path.lexists(path):
        return
    try:
        _tree_digest(policy, path)
        shutil.rmtree(path)
    except BaseException as error:
        if os.path.lexists(path):
            raise_as(
                policy.errors.cleanup,
                f"transaction tree cleanup failed: {path.name}",
                error,
            )
        raise
    if os.path.lexists(path):
        raise_as(policy.errors.cleanup, "transaction tree cleanup left residue")


def _remove_metadata(policy: ApplyPolicy, repo_root: Path) -> None:
    root = _transaction_root(policy, repo_root)
    try:
        for entry in list(os.scandir(root)):
            if entry.name == TRANSACTION_STATE_FILE or is_atomic_temporary_name(
                entry.name,
                TRANSACTION_STATE_FILE,
            ):
                metadata = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    raise MetadataError("transaction metadata was unsafe")
                Path(entry.path).unlink()
    except BaseException as error:
        raise_as(policy.errors.cleanup, "transaction metadata cleanup failed", error)


def _remove_empty_root(policy: ApplyPolicy, repo_root: Path) -> None:
    root = _transaction_root(policy, repo_root)
    if not os.path.lexists(root):
        return
    try:
        require_real_directory(root)
        if next(os.scandir(root), None) is not None:
            raise MetadataError("transaction root was not empty")
        root.rmdir()
    except BaseException as error:
        if os.path.lexists(root):
            raise_as(policy.errors.cleanup, "transaction root cleanup failed", error)
        raise
    if os.path.lexists(root):
        raise_as(policy.errors.cleanup, "transaction root cleanup left residue")


def _finish_recovery(
    policy: ApplyPolicy,
    repo_root: Path,
    fault_injector: FaultInjector | None,
) -> None:
    _remove_metadata(policy, repo_root)
    _fault(fault_injector, "after_state_delete")
    _fault(fault_injector, "before_transaction_root_delete")
    _remove_empty_root(policy, repo_root)


def _validate_recovery_target(
    policy: ApplyPolicy,
    repo_root: Path,
    record: _TransactionRecord,
    expected_digest: str,
    fault_injector: FaultInjector | None,
) -> None:
    policy.validator(repo_root)
    _fault(fault_injector, "after_validator")
    if _record_digest(policy, record, repo_root / policy.target_relative) != expected_digest:
        raise_as(
            policy.errors.ambiguous,
            "recovered target digest did not match expected digest",
        )
    _fault(fault_injector, "after_digest")


def recover_transaction(
    policy: ApplyPolicy,
    repo_root: Path,
    *,
    lock_path: Path | None = None,
    fault_injector: FaultInjector | None = None,
) -> TransactionInspection:
    repository = repo_root.resolve()
    stream = open_external_lock(
        policy,
        lock_path or policy.default_lock_path,
        repository,
    )
    try:
        _acquire_lock(policy, stream)
        inspection = inspect_transaction(policy, repository)
        if inspection.status == "CLEAN":
            return inspection
        if inspection.status == "AMBIGUOUS_RECOVERY_REQUIRED":
            raise_as(
                policy.errors.ambiguous,
                "ambiguous transaction cannot be recovered automatically",
            )
        if inspection.status == "EMPTY_TRANSACTION_ROOT_CLEANUP_PENDING":
            if not inspection.validator_pass:
                raise_as(
                    policy.errors.ambiguous,
                    "empty transaction root cannot be safely removed",
                )
            _fault(fault_injector, "before_transaction_root_delete")
            _remove_empty_root(policy, repository)
            return inspect_transaction(policy, repository)
        record = _read_record(policy, repository)
        target = repository / policy.target_relative
        if inspection.status == "PRE_INSTALL_RESIDUE":
            _validate_recovery_target(
                policy,
                repository,
                record,
                record.old_digest,
                fault_injector,
            )
            existed = os.path.lexists(record.staging)
            _remove_transaction_tree(policy, record.staging)
            if existed:
                _fault(fault_injector, "after_staging_delete")
            _finish_recovery(policy, repository, fault_injector)
        elif inspection.status in {
            "OLD_TREE_IN_BACKUP",
            "NEW_TREE_INSTALLED_UNVALIDATED",
            "OLD_TREE_RESTORED_CLEANUP_PENDING",
        }:
            _write_phase(
                policy,
                repository,
                record,
                "RECOVERING_OLD",
                fault_injector,
            )
            if (
                _record_digest(policy, record, target) == record.new_digest
                and _record_digest(policy, record, record.backup) == record.old_digest
                and not os.path.lexists(record.staging)
            ):
                os.replace(target, record.staging)
                _fault(fault_injector, "after_target_to_staging")
            if (
                not os.path.lexists(target)
                and _record_digest(policy, record, record.backup) == record.old_digest
            ):
                os.replace(record.backup, target)
                _fault(fault_injector, "after_backup_to_target")
            _validate_recovery_target(
                policy,
                repository,
                record,
                record.old_digest,
                fault_injector,
            )
            existed = os.path.lexists(record.staging)
            _remove_transaction_tree(policy, record.staging)
            if existed:
                _fault(fault_injector, "after_staging_delete")
            _write_phase(
                policy,
                repository,
                record,
                "OLD_RESTORED",
                fault_injector,
            )
            _finish_recovery(policy, repository, fault_injector)
        elif inspection.status in {
            "NEW_TREE_VALIDATED_BACKUP_RETAINED",
            "NEW_TREE_COMMITTED_CLEANUP_PENDING",
        }:
            _write_phase(
                policy,
                repository,
                record,
                "RECOVERING_NEW",
                fault_injector,
            )
            _validate_recovery_target(
                policy,
                repository,
                record,
                record.new_digest,
                fault_injector,
            )
            existed = os.path.lexists(record.backup)
            _remove_transaction_tree(policy, record.backup)
            if existed:
                _fault(fault_injector, "after_backup_delete")
            _write_phase(
                policy,
                repository,
                record,
                "NEW_COMMITTED",
                fault_injector,
            )
            _finish_recovery(policy, repository, fault_injector)
        else:
            raise_as(policy.errors.ambiguous, "unsupported recovery status")
    finally:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
    return inspect_transaction(policy, repository)


class ApplyGuard:
    """Hold one external lock around all replacement preconditions and effects."""

    def __init__(
        self,
        policy: ApplyPolicy,
        repo_root: Path,
        *,
        expected_base_sha: str,
        authority: ApplyAuthority | None = None,
        lock_path: Path | None = None,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        if SHA_RE.fullmatch(expected_base_sha) is None:
            raise_as(
                policy.errors.apply,
                "expected Base SHA must be a 40-character lowercase SHA",
            )
        selected_authority = authority or policy.default_authority
        if not isinstance(selected_authority, ApplyAuthority):
            raise_as(
                policy.errors.apply,
                "apply authority must be a fixed internal value",
            )
        self.policy = policy
        self.repo_root = repo_root.resolve()
        self.expected_base_sha = expected_base_sha
        self.authority = selected_authority
        self.lock_path = lock_path or policy.default_lock_path
        self.fault_injector = fault_injector
        self.target = self.repo_root / policy.target_relative
        self.transaction_root = _transaction_root(policy, self.repo_root)
        self.last_transaction_state: ApplyTransactionState | None = None
        self._lock: IO[str] | None = None
        self._branch = ""
        self._head = ""
        self._contract = ""
        self._target_digest = ""

    def __enter__(self) -> Self:
        self._lock = open_external_lock(
            self.policy,
            self.lock_path,
            self.repo_root,
        )
        try:
            _acquire_lock(self.policy, self._lock)
            inspection = inspect_transaction(self.policy, self.repo_root)
            if inspection.status != "CLEAN":
                raise_as(
                    self.policy.errors.residue,
                    "transaction residue requires manual review through inspect/recover",
                )
            self._contract = _contract_digest(self.policy, self.repo_root)
            self._target_digest = _tree_digest(self.policy, self.target)
            self._branch, self._head = _assert_git_guard(
                self.policy,
                self.repo_root,
                expected_base_sha=self.expected_base_sha,
                authority=self.authority,
            )
            if self.policy.prevalidate:
                self.policy.validator(self.repo_root)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def _stable(self) -> None:
        branch, head = _assert_git_guard(
            self.policy,
            self.repo_root,
            expected_base_sha=self.expected_base_sha,
            authority=self.authority,
            check_clean=False,
        )
        if branch != self._branch:
            raise_as(self.policy.errors.apply, "repository branch drifted")
        if head != self._head:
            raise_as(self.policy.errors.apply, "repository HEAD drifted")
        if _contract_digest(self.policy, self.repo_root) != self._contract:
            raise_as(
                self.policy.errors.contract_drift,
                "repository contract changed during collection",
            )
        if _tree_digest(self.policy, self.target) != self._target_digest:
            raise_as(
                self.policy.errors.apply,
                "formal target changed during collection",
            )
        if _worktree_changes(self.policy, self.repo_root):
            raise_as(
                self.policy.errors.dirty,
                "entire Git worktree must be clean for apply",
            )

    def assert_stable(self) -> None:
        self._stable()

    def _prepare_transaction_root(self) -> None:
        if os.path.lexists(self.transaction_root):
            raise_as(
                self.policy.errors.recovery_required,
                "stale transaction requires recovery",
            )
        try:
            self.transaction_root.mkdir(mode=TRANSACTION_ROOT_MODE)
            self.transaction_root.chmod(TRANSACTION_ROOT_MODE)
            metadata = require_real_directory(self.transaction_root)
            if stat.S_IMODE(metadata.st_mode) != TRANSACTION_ROOT_MODE:
                raise MetadataError("transaction root mode was invalid")
            if metadata.st_dev != self.target.parent.stat().st_dev:
                raise MetadataError("transaction root and target parent must share a filesystem")
        except (MetadataError, OSError) as error:
            raise_as(
                self.policy.errors.apply,
                "transaction root and formal target parent must share a filesystem",
                error,
            )

    def _new_record(
        self,
        *,
        transaction_id: str,
        new_digest: str,
    ) -> _TransactionRecord:
        root = self.transaction_root
        backup_name = f"backup-{transaction_id}"
        staging_name = f"staging-{transaction_id}"
        document: dict[str, object] = {
            "authority": TRANSACTION_AUTHORITY,
            "version": TRANSACTION_STATE_VERSION,
            "owner": self.policy.owner,
            "transaction_id": transaction_id,
            "phase": "STARTED",
            "target": self.policy.target_relative.as_posix(),
            "old_digest": self._target_digest,
            "new_digest": new_digest,
            "contract_digest": self._contract,
            "backup": backup_name,
            "staging": staging_name,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        return _TransactionRecord(
            document,
            transaction_id,
            "STARTED",
            self._target_digest,
            new_digest,
            self._contract,
            root / backup_name,
            root / staging_name,
            TRANSACTION_STATE_VERSION,
            None,
        )

    def _write_phase(self, record: _TransactionRecord, phase: str) -> None:
        _write_phase(
            self.policy,
            self.repo_root,
            record,
            phase,
            self.fault_injector,
        )

    def _automatic_restore(
        self,
        record: _TransactionRecord,
        state: ApplyTransactionState,
    ) -> None:
        target_digest = _existing_digest(self.policy, self.target)
        backup_digest = _existing_digest(self.policy, record.backup)
        staging_digest = _existing_digest(self.policy, record.staging)
        if backup_digest == record.old_digest:
            if target_digest == record.new_digest and staging_digest is None:
                os.replace(self.target, record.staging)
                state.staging_created = True
                target_digest = None
            if target_digest is None:
                os.replace(record.backup, self.target)
                state.old_tree_restored = True
            elif target_digest != record.old_digest:
                raise_as(
                    self.policy.errors.recovery_required,
                    "automatic recovery target facts were ambiguous",
                )
        elif backup_digest is not None or target_digest != record.old_digest:
            raise_as(
                self.policy.errors.recovery_required,
                "automatic recovery backup facts were ambiguous",
            )
        self.policy.validator(self.repo_root)
        if _tree_digest(self.policy, self.target) != record.old_digest:
            raise_as(
                self.policy.errors.recovery_required,
                "automatic recovery digest mismatched",
            )
        _remove_transaction_tree(self.policy, record.staging)
        state.staging_created = False
        _remove_metadata(self.policy, self.repo_root)
        _remove_empty_root(self.policy, self.repo_root)
        state.recovery_required = False

    def commit(
        self,
        source: Path,
        *,
        expected_source_digest: str | None = None,
    ) -> None:
        self._stable()
        new_digest = _tree_digest(self.policy, source)
        if expected_source_digest is not None and new_digest != expected_source_digest:
            raise_as(self.policy.errors.apply, "candidate source digest drifted")
        self._prepare_transaction_root()
        transaction_id = str(uuid.uuid4())
        record = self._new_record(
            transaction_id=transaction_id,
            new_digest=new_digest,
        )
        state = ApplyTransactionState()
        self.last_transaction_state = state
        self._write_phase(record, "STARTED")
        validation_failure: BaseException | None = None
        try:
            shutil.copytree(source, record.staging)
            state.staging_created = True
            if _tree_digest(self.policy, record.staging) != new_digest:
                raise_as(self.policy.errors.apply, "staged candidate drifted")
            self._write_phase(record, "STAGED")
            _fault(self.fault_injector, "after_staging")
            self._stable()
            os.replace(self.target, record.backup)
            state.old_tree_moved_to_backup = True
            self._write_phase(record, "BACKED_UP")
            _fault(self.fault_injector, "after_backup")
            os.replace(record.staging, self.target)
            state.staging_created = False
            state.new_tree_installed = True
            self._write_phase(record, "INSTALLED")
            _fault(self.fault_injector, "after_install")
            _fault(self.fault_injector, "before_post_install_validator")
            try:
                self.policy.validator(self.repo_root)
            except BaseException as error:
                validation_failure = error
                raise
            _fault(self.fault_injector, "after_post_install_validator")
            if _tree_digest(self.policy, self.target) != new_digest:
                raise_as(self.policy.errors.apply, "installed target drifted")
            state.new_tree_validated = True
            self._write_phase(record, "VALIDATED")
            _fault(self.fault_injector, "after_validation")
        except BaseException as original_error:
            state.recovery_required = True
            try:
                self._automatic_restore(record, state)
            except BaseException as recovery_error:
                if isinstance(recovery_error, self.policy.errors.cleanup):
                    raise recovery_error from original_error
                preserved = [
                    path.relative_to(self.repo_root).as_posix()
                    for path in (record.backup, record.staging, self.target)
                    if os.path.lexists(path)
                ]
                detail = ",".join(preserved) if preserved else "unavailable"
                raise_as(
                    self.policy.errors.recovery_required,
                    f"apply recovery requires explicit inspection; preserved paths: {detail}",
                    recovery_error,
                )
            if validation_failure is not None and self.policy.errors.post_validation:
                raise self.policy.errors.post_validation(validation_failure) from None
            if isinstance(original_error, Exception):
                raise_as(
                    self.policy.errors.apply,
                    "apply failed; prior data was restored",
                )
            raise
        _remove_transaction_tree(self.policy, record.backup)
        state.backup_cleanup_complete = True
        _fault(self.fault_injector, "after_backup_cleanup")
        _remove_metadata(self.policy, self.repo_root)
        _fault(self.fault_injector, "after_state_cleanup")
        _fault(self.fault_injector, "before_transaction_root_cleanup")
        _remove_empty_root(self.policy, self.repo_root)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._lock is not None:
            try:
                fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
            finally:
                self._lock.close()
                self._lock = None
