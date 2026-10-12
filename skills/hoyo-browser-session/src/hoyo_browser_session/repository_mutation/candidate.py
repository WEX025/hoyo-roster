"""Owned private Candidate workspace lifecycle."""

from __future__ import annotations

import os
import re
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from .common import (
    MetadataError,
    atomic_write_metadata,
    copy_regular_file,
    is_atomic_temporary_name,
    load_metadata,
    raise_as,
    remove_owned_tree,
    require_real_directory,
    secure_private_directory,
)

CANDIDATE_AUTHORITY = "hoyo-repository-mutation/candidate-v2"
CANDIDATE_STATE_VERSION = 2
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

LegacyCandidateFormat = Literal["owner-v1", "zzz-v1"]
FaultInjector = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class CandidateErrors:
    validation: type[Exception]
    cleanup: type[Exception]
    inspection: type[Exception]
    residue: type[Exception]


@dataclass(frozen=True, slots=True)
class CandidatePolicy:
    owner: str
    candidate_base: Path
    target_relative: Path
    contract_paths: tuple[Path, ...]
    errors: CandidateErrors
    legacy_format: LegacyCandidateFormat
    legacy_owners: tuple[str, ...] = ()
    legacy_validated_has_contract_digest: bool = False
    state_file: str = "candidate-state.json"
    mode: int = 0o700

    def __post_init__(self) -> None:
        if (
            not self.owner
            or self.owner != self.owner.strip()
            or self.target_relative.is_absolute()
            or ".." in self.target_relative.parts
            or not self.target_relative.parts
        ):
            raise ValueError("candidate policy was invalid")


@dataclass(frozen=True, slots=True)
class CandidateEvidence:
    game_tree_digest: str
    business_digest: str
    contract_digest: str

    def __post_init__(self) -> None:
        for value in (
            self.game_tree_digest,
            self.business_digest,
            self.contract_digest,
        ):
            if SHA256_RE.fullmatch(value) is None:
                raise ValueError("candidate evidence digest was invalid")


@dataclass(frozen=True, slots=True)
class CandidateInspection:
    run_id: str
    status: str
    cleanup_allowed: bool
    game_tree_digest: str | None = None
    business_digest: str | None = None
    contract_digest: str | None = None
    metadata_version: int | None = None


@dataclass(frozen=True, slots=True)
class CandidateWorkspace:
    root: Path
    base: Path
    policy: CandidatePolicy

    @property
    def target_dir(self) -> Path:
        return self.root / self.policy.target_relative

    def cleanup(self) -> None:
        if os.path.lexists(self.root):
            cleanup_candidate(
                self.policy,
                self.root.name,
                candidate_base=self.base,
            )


def _fault(injector: FaultInjector | None, *stages: str) -> None:
    if injector is None:
        return
    for stage in stages:
        injector(stage)


def _base(
    policy: CandidatePolicy,
    candidate_base: Path | None,
    *,
    repo_root: Path | None = None,
) -> Path:
    try:
        return secure_private_directory(
            candidate_base or policy.candidate_base,
            repository_root=repo_root,
        )
    except MetadataError as error:
        raise_as(policy.errors.inspection, "candidate base was unsafe", error)


def _canonical_uuid(value: str) -> str:
    canonical = str(uuid.UUID(value))
    if canonical != value:
        raise ValueError
    return canonical


def _valid_digest(value: object) -> str:
    if type(value) is not str or SHA256_RE.fullmatch(value) is None:
        raise ValueError
    return value


def _inspect_v2(
    policy: CandidatePolicy,
    run_id: str,
    state: dict[str, object],
) -> CandidateInspection:
    common = {
        "authority": CANDIDATE_AUTHORITY,
        "version": CANDIDATE_STATE_VERSION,
        "owner": state.get("owner"),
        "run_id": run_id,
        "status": state.get("status"),
    }
    if (
        state.get("authority") != CANDIDATE_AUTHORITY
        or type(state.get("version")) is not int
        or state["version"] != CANDIDATE_STATE_VERSION
        or state.get("owner") != policy.owner
        or state.get("run_id") != run_id
        or type(state.get("status")) is not str
    ):
        raise ValueError
    status = str(state["status"])
    if status == "building" and state == common:
        return CandidateInspection(
            run_id,
            "BUILDING",
            True,
            metadata_version=CANDIDATE_STATE_VERSION,
        )
    expected = {
        *common,
        "game_tree_digest",
        "business_digest",
        "contract_digest",
    }
    if status != "validated" or set(state) != expected:
        raise ValueError
    return CandidateInspection(
        run_id,
        "VALIDATED",
        True,
        _valid_digest(state["game_tree_digest"]),
        _valid_digest(state["business_digest"]),
        _valid_digest(state["contract_digest"]),
        CANDIDATE_STATE_VERSION,
    )


def _inspect_owner_v1(
    policy: CandidatePolicy,
    run_id: str,
    state: dict[str, object],
) -> CandidateInspection:
    status = state.get("status")
    owner = state.get("owner")
    common = {
        "version": 1,
        "owner": owner,
        "run_id": run_id,
        "status": status,
    }
    if (
        type(state.get("version")) is not int
        or state["version"] != 1
        or owner not in policy.legacy_owners
        or state.get("run_id") != run_id
        or type(status) is not str
    ):
        raise ValueError
    if status == "building" and state == common:
        return CandidateInspection(run_id, "BUILDING", True, metadata_version=1)
    base_fields = {
        *common,
        "game_tree_digest",
        "business_digest",
    }
    expected = (
        {*base_fields, "contract_digest"}
        if policy.legacy_validated_has_contract_digest
        else base_fields
    )
    if status != "validated" or set(state) != expected:
        raise ValueError
    contract = (
        _valid_digest(state["contract_digest"])
        if policy.legacy_validated_has_contract_digest
        else None
    )
    return CandidateInspection(
        run_id,
        "VALIDATED",
        True,
        _valid_digest(state["game_tree_digest"]),
        _valid_digest(state["business_digest"]),
        contract,
        1,
    )


def _inspect_zzz_v1(
    run_id: str,
    state: dict[str, object],
) -> CandidateInspection:
    if set(state) != {"version", "run_id", "status", "created_at"}:
        raise ValueError
    status = state.get("status")
    created_at = state.get("created_at")
    if (
        type(state.get("version")) is not int
        or state["version"] != 1
        or state.get("run_id") != run_id
        or status not in {"building", "validated"}
        or type(created_at) is not str
    ):
        raise ValueError
    datetime.fromisoformat(created_at)
    return CandidateInspection(
        run_id,
        str(status).upper(),
        True,
        metadata_version=1,
    )


def inspect_candidate(
    policy: CandidatePolicy,
    path: Path,
) -> CandidateInspection:
    try:
        run_id = _canonical_uuid(path.name)
    except ValueError:
        return CandidateInspection("UNKNOWN", "UNOWNED", False)
    try:
        metadata = require_real_directory(path)
        if stat.S_IMODE(metadata.st_mode) != policy.mode:
            raise ValueError
        state_path = path / policy.state_file
        if not os.path.lexists(state_path):
            children = list(os.scandir(path))
            if all(
                is_atomic_temporary_name(item.name, policy.state_file)
                and stat.S_ISREG(item.stat(follow_symlinks=False).st_mode)
                and not stat.S_ISLNK(item.stat(follow_symlinks=False).st_mode)
                for item in children
            ):
                return CandidateInspection(
                    run_id,
                    "ORPHANED_INITIALIZATION",
                    True,
                )
            raise ValueError
        value = load_metadata(state_path)
        if not isinstance(value, dict):
            raise ValueError
        state = dict(value)
        if state.get("authority") == CANDIDATE_AUTHORITY:
            return _inspect_v2(policy, run_id, state)
        if policy.legacy_format == "owner-v1":
            return _inspect_owner_v1(policy, run_id, state)
        return _inspect_zzz_v1(run_id, state)
    except (KeyError, MetadataError, OSError, ValueError, AttributeError):
        return CandidateInspection(run_id, "UNOWNED", False)


def inspect_candidates(
    policy: CandidatePolicy,
    *,
    candidate_base: Path | None = None,
) -> tuple[CandidateInspection, ...]:
    path = (candidate_base or policy.candidate_base).expanduser().absolute()
    if not os.path.lexists(path):
        return ()
    base = _base(policy, candidate_base)
    try:
        entries = sorted(os.scandir(base), key=lambda entry: entry.name)
    except OSError as error:
        raise_as(policy.errors.inspection, "candidate base could not be listed", error)
    return tuple(inspect_candidate(policy, Path(entry.path)) for entry in entries)


def assert_no_stale_candidates(
    policy: CandidatePolicy,
    *,
    candidate_base: Path | None = None,
) -> None:
    if inspect_candidates(policy, candidate_base=candidate_base):
        raise_as(
            policy.errors.residue,
            "stale candidate requires inspect and explicit cleanup",
        )


def cleanup_candidate(
    policy: CandidatePolicy,
    run_id: str,
    *,
    candidate_base: Path | None = None,
) -> None:
    base = _base(policy, candidate_base)
    try:
        canonical = _canonical_uuid(run_id)
    except ValueError as error:
        raise_as(
            policy.errors.cleanup,
            "candidate cleanup requires a canonical UUID",
            error,
        )
    path = base / canonical
    if path.parent != base or not os.path.lexists(path):
        raise_as(policy.errors.cleanup, "candidate cleanup target does not exist")
    inspection = inspect_candidate(policy, path)
    if not inspection.cleanup_allowed:
        raise_as(policy.errors.cleanup, "candidate ownership was not proven")
    try:
        remove_owned_tree(path)
    except BaseException as error:
        if os.path.lexists(path):
            raise_as(policy.errors.cleanup, "candidate cleanup failed", error)
        raise


def create_candidate_workspace(
    policy: CandidatePolicy,
    repo_root: Path,
    *,
    populate: Callable[[CandidateWorkspace], None],
    validate: Callable[[Path], None],
    evidence: Callable[[CandidateWorkspace], CandidateEvidence],
    candidate_base: Path | None = None,
    fault_injector: FaultInjector | None = None,
) -> CandidateWorkspace:
    repository = repo_root.resolve()
    base = _base(policy, candidate_base, repo_root=repository)
    assert_no_stale_candidates(policy, candidate_base=base)
    run_id = str(uuid.uuid4())
    candidate_root = base / run_id
    try:
        candidate_root.mkdir(mode=policy.mode)
        candidate_root.chmod(policy.mode)
        if stat.S_IMODE(require_real_directory(candidate_root).st_mode) != policy.mode:
            raise MetadataError("candidate run directory mode was invalid")
    except (MetadataError, OSError) as error:
        raise_as(policy.errors.inspection, "candidate workspace creation failed", error)
    workspace = CandidateWorkspace(candidate_root, base, policy)
    try:
        _fault(
            fault_injector,
            "candidate_workspace_created",
            "after_mkdir",
        )
        atomic_write_metadata(
            candidate_root / policy.state_file,
            {
                "authority": CANDIDATE_AUTHORITY,
                "version": CANDIDATE_STATE_VERSION,
                "owner": policy.owner,
                "run_id": run_id,
                "status": "building",
            },
            before_replace=lambda temporary: _fault(
                fault_injector,
                "candidate_metadata_persisting",
                "during_state_write",
            ),
        )
        _fault(
            fault_injector,
            "candidate_metadata_persisted",
            "after_state",
            "after_root",
        )
        for relative in policy.contract_paths:
            try:
                copy_regular_file(
                    repository / relative,
                    candidate_root / relative,
                )
            except (MetadataError, OSError) as error:
                raise_as(
                    policy.errors.validation,
                    "candidate contract source was unsafe",
                    error,
                )
        populate(workspace)
        validate(candidate_root)
        confirmed = evidence(workspace)
        atomic_write_metadata(
            candidate_root / policy.state_file,
            {
                "authority": CANDIDATE_AUTHORITY,
                "version": CANDIDATE_STATE_VERSION,
                "owner": policy.owner,
                "run_id": run_id,
                "status": "validated",
                "game_tree_digest": confirmed.game_tree_digest,
                "business_digest": confirmed.business_digest,
                "contract_digest": confirmed.contract_digest,
            },
        )
        _fault(fault_injector, "candidate_validated_metadata_persisted")
        return workspace
    except BaseException as original_error:
        try:
            workspace.cleanup()
        except BaseException as cleanup_error:
            raise cleanup_error from original_error
        raise
