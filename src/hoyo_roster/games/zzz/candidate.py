"""ZZZ Candidate policy over the shared workspace mechanism."""

from __future__ import annotations

import hashlib
import os
import shutil  # noqa: F401 - compatibility for existing fault-injection callers
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hoyo_roster.blocking import command_timeout
from hoyo_roster.storage.candidate import (
    CandidateErrors,
    CandidateEvidence,
    CandidateInspection,
    CandidatePolicy,
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
    inspect_candidate as _inspect_candidate,
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

from .errors import (
    CandidateCleanupError,
    CandidateInspectionError,
    CandidateResidueError,
    CandidateValidationError,
    SnapshotConsistencyError,
)
from .ids import parse_character_id
from .jsonio import StrictJsonError, atomic_write_json, load_json
from .normalize import NormalizedSnapshot

ZZZ_RELATIVE = Path("data/zenless-zone-zero")
CANDIDATE_BASE_ROOT = Path("~/.local/share/hoyo-roster/candidates/zzz-character-state-sync")
CANDIDATE_ROOT_MODE = 0o700
CANDIDATE_STATE_FILE = "candidate-state.json"
CANDIDATE_STATE_VERSION = 2
MUTATION_OWNER = "zzz-character-state-sync"
CANDIDATE_OWNER = MUTATION_OWNER
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


def _policy() -> CandidatePolicy:
    return CandidatePolicy(
        owner=CANDIDATE_OWNER,
        candidate_base=CANDIDATE_BASE_ROOT,
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
    command = [sys.executable, "tools/validate_repository.py", "--root", str(root)]
    if scope_game is not None:
        command.extend(("--scope-game", scope_game))
    try:
        result = subprocess.run(
            command,
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=command_timeout(90),
        )
    except subprocess.TimeoutExpired as error:
        raise CandidateValidationError("repository Validator timed out") from error
    if result.returncode != 0:
        diagnostic = hashlib.sha256()
        diagnostic.update(result.stdout.encode("utf-8"))
        diagnostic.update(b"\0")
        diagnostic.update(result.stderr.encode("utf-8"))
        raise CandidateValidationError(
            "repository Validator rejected the candidate",
            diagnostic_sha256=diagnostic.hexdigest(),
        )


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


def _inspection(path: Path) -> CandidateInspection:
    return _inspect_candidate(_policy(), path)


def inspect_candidates(
    *,
    candidate_base: Path | None = None,
) -> tuple[CandidateInspection, ...]:
    return _inspect_candidates(_policy(), candidate_base=candidate_base)


def assert_no_stale_candidates(*, candidate_base: Path | None = None) -> None:
    _assert_no_stale_candidates(_policy(), candidate_base=candidate_base)


def cleanup_candidate(
    run_id: str,
    *,
    candidate_base: Path | None = None,
) -> None:
    _cleanup_candidate(_policy(), run_id, candidate_base=candidate_base)


@dataclass(slots=True)
class Candidate:
    root: Path
    base_root: Path | None = None

    @property
    def run_id(self) -> str:
        return self.root.name

    @property
    def zzz_dir(self) -> Path:
        return self.root / ZZZ_RELATIVE

    def cleanup(self) -> None:
        if not os.path.lexists(self.root):
            return
        cleanup_candidate(
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
    fault_injector: Callable[[str], None] | None = None,
) -> Candidate:
    """Create and validate the game-scoped ZZZ candidate closure."""

    root = repo_root.resolve()
    run_repository_validator(root)
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
            business_digest=target_digest,
            contract_digest=repository_contract_digest,
        )

    workspace = create_candidate_workspace(
        _policy(),
        root,
        populate=populate,
        validate=validate,
        evidence=evidence,
        candidate_base=candidate_base,
        fault_injector=fault_injector,
    )
    return Candidate(workspace.root, workspace.base)
