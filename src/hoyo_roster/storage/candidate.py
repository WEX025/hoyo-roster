"""Owned private Candidate workspace lifecycle."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .common import (
    MetadataError,
    atomic_write_metadata,
    copy_regular_file,
    load_metadata,
    raise_as,
    remove_owned_tree,
    require_real_directory,
    secure_private_directory,
    tree_digest,
    files_digest,
)

CANDIDATE_AUTHORITY = "hoyo-repository-mutation/candidate-v3"
CANDIDATE_STATE_VERSION = 3
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

FaultInjector = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class CandidateErrors:
    validation: type[Exception]
    cleanup: type[Exception]
    inspection: type[Exception]
    residue: type[Exception]


REGIONS = {
    "genshin-impact": ("cn_gf01", "cn_qd01"),
    "zenless-zone-zero": ("prod_gf_cn",),
    "honkai-star-rail": ("prod_gf_cn",),
}


@dataclass(frozen=True, slots=True)
class CandidateScope:
    """An explicit identity selector; region is optional only during offline inspection."""

    game: str
    uid_digest: str
    region: str | None = None

    def __post_init__(self) -> None:
        if (
            self.game not in REGIONS
            or SHA256_RE.fullmatch(self.uid_digest) is None
            or (self.region is not None and self.region not in REGIONS[self.game])
        ):
            raise ValueError("candidate scope was invalid")

    @classmethod
    def identity(cls, game: str, uid: str, region: str | None = None) -> CandidateScope:
        if not isinstance(uid, str) or re.fullmatch(r"[1-9][0-9]{0,31}", uid) is None:
            raise ValueError("candidate identity was invalid")
        return cls(game, hashlib.sha256(f"hoyo-roster:{game}:{uid}".encode()).hexdigest(), region)

    @classmethod
    def profile(cls, profile: dict) -> CandidateScope:
        scope = cls.identity(
            profile["game"], profile["account"]["uid"], profile["account"]["server"]
        )
        if scope.region is None:
            raise ValueError("candidate region was required")
        return scope

    def document(self) -> dict[str, str]:
        if self.region is None:
            raise ValueError("candidate region was required")
        return {
            "project": "hoyo-roster",
            "game": self.game,
            "uid_digest": self.uid_digest,
            "region": self.region,
        }

    def accepts(self, scope: CandidateScope) -> bool:
        return (
            self.game == scope.game
            and self.uid_digest == scope.uid_digest
            and (self.region is None or self.region == scope.region)
        )


@dataclass(frozen=True, slots=True)
class CandidatePolicy:
    owner: str
    candidate_base: Path
    target_relative: Path
    contract_paths: tuple[Path, ...]
    errors: CandidateErrors
    scope: CandidateScope | None = None
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
        if self.scope is not None and self.scope.game != self.target_relative.name:
            raise ValueError("candidate policy game did not match its target")


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


def _inspect_v3(
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
        "scope": state.get("scope"),
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
    value = state["scope"]
    if not isinstance(value, dict) or set(value) != {"project", "game", "uid_digest", "region"}:
        raise ValueError
    actual = CandidateScope(value["game"], value["uid_digest"], value["region"])
    if value != actual.document():
        raise ValueError
    if policy.scope is None:
        raise ValueError
    if not policy.scope.accepts(actual):
        return CandidateInspection(run_id, "SCOPE_MISMATCH", False)
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
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != policy.mode:
            raise ValueError
        state_path = path / policy.state_file
        if not os.path.lexists(state_path):
            # Directory shape and atomic temporary names are never ownership evidence.
            raise ValueError
        for item in (state_path, *path.rglob("*")):
            metadata = item.lstat()
            if (
                metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode)
                != (0o700 if stat.S_ISDIR(metadata.st_mode) else 0o600)
                or not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode))
            ):
                raise ValueError
        value = load_metadata(state_path)
        if not isinstance(value, dict):
            raise ValueError
        state = dict(value)
        if state.get("authority") == CANDIDATE_AUTHORITY:
            inspection = _inspect_v3(policy, run_id, state)
            if not inspection.cleanup_allowed:
                return inspection
            if inspection.status == "VALIDATED":
                known = (*policy.contract_paths, policy.target_relative, Path(policy.state_file))
                for item in path.rglob("*"):
                    relative = item.relative_to(path)
                    if not any(
                        relative == name
                        or name.is_relative_to(relative)
                        or relative.is_relative_to(policy.target_relative)
                        for name in known
                    ):
                        raise ValueError
            profile_path = path / policy.target_relative / "profile.json"
            if os.path.lexists(profile_path):
                profile = load_metadata(profile_path)
                if CandidateScope.profile(profile).document() != state["scope"]:
                    return CandidateInspection(run_id, "SCOPE_MISMATCH", False)
            elif inspection.status == "VALIDATED":
                raise ValueError
            if inspection.status == "VALIDATED" and (
                tree_digest(path / policy.target_relative) != inspection.game_tree_digest
                or files_digest(path, policy.contract_paths) != inspection.contract_digest
            ):
                raise ValueError
            return inspection
        raise ValueError
    except (KeyError, MetadataError, OSError, ValueError, TypeError, AttributeError):
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
        raise_as(
            policy.errors.cleanup,
            "CANDIDATE_SCOPE_MISMATCH"
            if inspection.status == "SCOPE_MISMATCH"
            else "CANDIDATE_OWNERSHIP_UNPROVEN",
        )
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
    if policy.scope is None or policy.scope.region is None:
        raise_as(policy.errors.validation, "candidate creation requires an exact identity scope")
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
                "scope": policy.scope.document(),
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
        for item in candidate_root.rglob("*"):
            metadata = item.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode))
            ):
                raise MetadataError("unsafe candidate path")
            item.chmod(0o700 if stat.S_ISDIR(metadata.st_mode) else 0o600)
        validate(candidate_root)
        confirmed = evidence(workspace)
        atomic_write_metadata(
            candidate_root / policy.state_file,
            {
                "authority": CANDIDATE_AUTHORITY,
                "version": CANDIDATE_STATE_VERSION,
                "owner": policy.owner,
                "scope": policy.scope.document(),
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
