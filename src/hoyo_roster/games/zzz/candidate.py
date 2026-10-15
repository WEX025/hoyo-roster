"""ZZZ Candidate policy over the shared workspace mechanism."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hoyo_roster.storage.candidate import (
    CandidateErrors,
    CandidateEvidence,
    CandidateInspection,
    CandidatePolicy,
    CandidateScope,
    CandidateWorkspace,
    create_candidate_workspace,
)
from hoyo_roster.storage.candidate import (
    assert_no_stale_candidates as _assert_no_stale_candidates,
)
from hoyo_roster.storage.candidate import (
    cleanup_candidate as _cleanup_candidate,
)
from hoyo_roster.storage.candidate import (
    inspect_candidates as _inspect_candidates,
)
from hoyo_roster.storage.common import (
    MetadataError,
    copy_regular_file,
    files_digest,
)
from hoyo_roster.storage.common import (
    tree_digest as _shared_tree_digest,
)

from hoyo_roster.validation import (
    ValidationFailure,
    run_validator as validate_contract,
    validate_game_source,
)

from .errors import (
    CandidateCleanupError,
    CandidateInspectionError,
    CandidateResidueError,
    CandidateValidationError,
    SnapshotConsistencyError,
)
from .ids import parse_character_id
from hoyo_roster.jsonio import StrictJsonError, atomic_write_json, load_json
from .normalize import NormalizedSnapshot

ZZZ_RELATIVE = Path("data/zenless-zone-zero")
CANDIDATE_OWNER = "zzz-character-state-sync"
CANDIDATE_SCHEMA_PATHS = (
    Path("schemas/common/definitions.schema.json"),
    Path("schemas/repository-manifest.schema.json"),
    Path("schemas/zenless-zone-zero/profile.schema.json"),
    Path("schemas/zenless-zone-zero/roster.schema.json"),
    Path("schemas/zenless-zone-zero/build.schema.json"),
    Path("schemas/zenless-zone-zero/bangboos.schema.json"),
)
CONTRACT_PATHS = (
    Path("data/manifest.json"),
    Path("tools/validate_repository.py"),
    *CANDIDATE_SCHEMA_PATHS,
)


def candidate_policy(scope: CandidateScope | None = None) -> CandidatePolicy:
    return CandidatePolicy(
        owner=CANDIDATE_OWNER,
        scope=scope,
        candidate_base=None,
        target_relative=ZZZ_RELATIVE,
        contract_paths=CONTRACT_PATHS,
        errors=CandidateErrors(
            validation=CandidateValidationError,
            cleanup=CandidateCleanupError,
            inspection=CandidateInspectionError,
            residue=CandidateResidueError,
        ),
    )


def write_json(path: Path, value: Any) -> None:
    atomic_write_json(path, value)


def _copy_regular_file(source: Path, destination: Path) -> None:
    try:
        copy_regular_file(source, destination)
    except MetadataError as error:
        raise CandidateValidationError("candidate source file was unsafe") from error


def tree_digest(path: Path) -> str:
    try:
        return _shared_tree_digest(path)
    except MetadataError as error:
        raise CandidateValidationError("tree inspection failed") from error


def contract_digest(root: Path) -> str:
    try:
        return files_digest(root, CONTRACT_PATHS)
    except MetadataError as error:
        raise CandidateValidationError("candidate contract source was unsafe") from error


def run_repository_validator(root: Path, *, scope_game: str | None = None) -> None:
    try:
        validate_contract(root, scope_game=scope_game)
    except ValidationFailure as error:
        raise CandidateValidationError(error.code, diagnostic_sha256=error.diagnostic) from None


def _check_nonshrinking_roster(
    repo_root: Path,
    snapshot: NormalizedSnapshot,
) -> None:
    try:
        profile = load_json(repo_root / ZZZ_RELATIVE / "profile.json")
        roster = load_json(repo_root / ZZZ_RELATIVE / "roster.json")
    except (OSError, StrictJsonError) as error:
        raise CandidateValidationError("formal ZZZ data is not strict JSON") from error
    if not isinstance(profile, dict) or profile.get("sync", {}).get("state") not in {
        "partial",
        "complete",
    }:
        return
    if not isinstance(roster, dict):
        raise CandidateValidationError("formal ZZZ roster is invalid")
    old_ids = set(roster.get("characters", {}))
    new_ids = set(snapshot.roster["characters"])
    if not old_ids <= new_ids:
        raise SnapshotConsistencyError("new complete roster is missing a prior character ID")


def inspect_candidates(
    *,
    candidate_base: Path | None = None,
    uid: str | None = None,
    region: str | None = None,
) -> tuple[CandidateInspection, ...]:
    return _inspect_candidates(
        candidate_policy(
            CandidateScope.identity("zenless-zone-zero", uid, region) if uid else None
        ),
        candidate_base=candidate_base,
    )


def assert_no_stale_candidates(*, candidate_base: Path | None = None) -> None:
    _assert_no_stale_candidates(candidate_policy(), candidate_base=candidate_base)


def cleanup_candidate(
    run_id: str,
    *,
    candidate_base: Path | None = None,
    uid: str | None = None,
    region: str | None = None,
) -> None:
    _cleanup_candidate(
        candidate_policy(
            CandidateScope.identity("zenless-zone-zero", uid, region) if uid else None
        ),
        run_id,
        candidate_base=candidate_base,
    )


@dataclass(slots=True)
class Candidate:
    root: Path
    scope: CandidateScope
    base_root: Path | None = None

    @property
    def run_id(self) -> str:
        return self.root.name

    @property
    def zzz_dir(self) -> Path:
        return self.root / ZZZ_RELATIVE

    @property
    def game_dir(self) -> Path:
        return self.zzz_dir

    def cleanup(self) -> None:
        if not os.path.lexists(self.root):
            return
        _cleanup_candidate(
            candidate_policy(self.scope),
            self.run_id,
            candidate_base=self.base_root or self.root.parent,
        )

    @property
    def removed(self) -> bool:
        return not os.path.lexists(self.root)


def _safe_build_path(builds_dir: Path, character_id: str) -> Path:
    normalized_id = parse_character_id(
        character_id,
        error_type=SnapshotConsistencyError,
    )
    if (
        normalized_id in {".", ".."}
        or "/" in normalized_id
        or "\\" in normalized_id
        or Path(normalized_id).is_absolute()
        or len(normalized_id) > 32
    ):
        raise CandidateValidationError("candidate Build path component is unsafe")
    resolved_builds = builds_dir.resolve(strict=True)
    resolved_build = (builds_dir / f"{normalized_id}.json").resolve(strict=False)
    if resolved_build.parent != resolved_builds or not resolved_build.is_relative_to(
        resolved_builds
    ):
        raise CandidateValidationError("candidate Build path escaped the builds directory")
    return resolved_build


def create_candidate(
    repo_root: Path,
    snapshot: NormalizedSnapshot,
    *,
    candidate_base: Path | None = None,
    uid: str | None = None,
    fault_injector: Callable[[str], None] | None = None,
) -> Candidate:
    """Create and validate the game-scoped ZZZ candidate closure."""

    scope = CandidateScope.profile(snapshot.profile)
    if uid is not None and not CandidateScope.identity(scope.game, uid).accepts(scope):
        raise CandidateValidationError("CANDIDATE_SCOPE_MISMATCH")
    root = repo_root.resolve()
    try:
        validate_game_source(root, "zenless-zone-zero")
    except ValidationFailure as error:
        raise CandidateValidationError(error.code, diagnostic_sha256=error.diagnostic) from None
    _check_nonshrinking_roster(root, snapshot)

    def populate(workspace: CandidateWorkspace) -> None:
        candidate_zzz = workspace.target_dir
        builds_dir = candidate_zzz / "builds"
        builds_dir.mkdir(parents=True)
        try:
            _copy_regular_file(
                root / ZZZ_RELATIVE / "builds/.gitkeep",
                builds_dir / ".gitkeep",
            )
        except MetadataError as error:
            raise CandidateValidationError("candidate Build placeholder was unsafe") from error
        write_json(candidate_zzz / "profile.json", snapshot.profile)
        write_json(candidate_zzz / "roster.json", snapshot.roster)
        write_json(candidate_zzz / "bangboos.json", snapshot.bangboos)
        for character_id, build in sorted(snapshot.builds.items()):
            write_json(_safe_build_path(builds_dir, character_id), build)
        if fault_injector is not None:
            fault_injector("after_partial_candidate")

    def validate(candidate_root: Path) -> None:
        run_repository_validator(candidate_root, scope_game="zenless-zone-zero")

    def evidence(workspace: CandidateWorkspace) -> CandidateEvidence:
        target_digest = tree_digest(workspace.target_dir)
        repository_contract_digest = contract_digest(root)
        if contract_digest(workspace.root) != repository_contract_digest:
            raise CandidateValidationError("candidate contract copy drifted")
        return CandidateEvidence(
            game_tree_digest=target_digest,
            contract_digest=repository_contract_digest,
        )

    workspace = create_candidate_workspace(
        candidate_policy(scope),
        root,
        populate=populate,
        validate=validate,
        evidence=evidence,
        candidate_base=candidate_base,
        fault_injector=fault_injector,
    )
    return Candidate(workspace.root, workspace.policy.scope, workspace.base)
