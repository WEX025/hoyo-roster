"""HSR Candidate policy over the shared workspace mechanism."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from hoyo_browser_session.blocking import command_timeout
from hoyo_browser_session.repository_mutation.candidate import (
    CandidateErrors,
    CandidateEvidence,
    CandidateInspection,
    CandidatePolicy,
    CandidateWorkspace,
    create_candidate_workspace,
)
from hoyo_browser_session.repository_mutation.candidate import (
    assert_no_stale_candidates as _assert_no_stale_candidates,
)
from hoyo_browser_session.repository_mutation.candidate import (
    cleanup_candidate as _cleanup_candidate,
)
from hoyo_browser_session.repository_mutation.candidate import (
    inspect_candidate as _inspect_candidate,
)
from hoyo_browser_session.repository_mutation.candidate import (
    inspect_candidates as _inspect_candidates,
)
from hoyo_browser_session.repository_mutation.common import (
    MetadataError,
    copy_regular_file,
    files_digest,
)
from hoyo_browser_session.repository_mutation.common import (
    tree_digest as _shared_tree_digest,
)

from .errors import (
    CandidateCleanupError,
    CandidateValidationError,
    FormalRepositoryPrevalidationError,
)
from .jsonio import atomic_write_json
from .lifecycle import HSR_RELATIVE, load_formal, snapshot_from_files
from .model import Snapshot
from .snapshot import business_digest

CANDIDATE_BASE_ROOT = Path("~/.local/share/hoyo-game-data/candidates/hsr-character-state-sync")
CANDIDATE_MODE = 0o700
STATE_FILE = "candidate-state.json"
CANDIDATE_STATE_VERSION = 2
MUTATION_OWNER = "hsr-character-state-sync"
LEGACY_OWNER_V1 = (
    "hsr-character-state-sync/1.0.0",
    "hsr-character-state-sync/1.0.1",
    "hsr-character-state-sync/1.0.2",
    "hsr-character-state-sync/1.0.3",
    "hsr-character-state-sync/1.0.4",
    "hsr-character-state-sync/1.0.5",
    "hsr-character-state-sync/1.0.6",
    "hsr-character-state-sync/1.0.7",
    "hsr-character-state-sync/1.0.8",
    "hsr-character-state-sync/1.1.0",
    "hsr-character-state-sync/1.1.1",
    "hsr-character-state-sync/1.1.2",
    "hsr-character-state-sync/1.1.3",
    "hsr-character-state-sync/1.1.4",
    "hsr-character-state-sync/1.1.5",
    "hsr-character-state-sync/1.1.6",
    "hsr-character-state-sync/1.1.7",
    "hsr-character-state-sync/1.1.8",
    "hsr-character-state-sync/1.1.9",
)
CANDIDATE_OWNER = MUTATION_OWNER
LEGACY_CANDIDATE_OWNERS = LEGACY_OWNER_V1
SCHEMA_PATHS = (
    Path("schemas/common/definitions.schema.json"),
    Path("schemas/repository-manifest.schema.json"),
    Path("schemas/honkai-star-rail/profile.schema.json"),
    Path("schemas/honkai-star-rail/roster.schema.json"),
    Path("schemas/honkai-star-rail/build.schema.json"),
)
CONTRACT_PATHS = (
    Path("data/manifest.json"),
    Path("tools/validate_repository.py"),
    *SCHEMA_PATHS,
)


def _policy() -> CandidatePolicy:
    return CandidatePolicy(
        owner=CANDIDATE_OWNER,
        candidate_base=CANDIDATE_BASE_ROOT,
        target_relative=HSR_RELATIVE,
        contract_paths=CONTRACT_PATHS,
        errors=CandidateErrors(
            validation=CandidateValidationError,
            cleanup=CandidateCleanupError,
            inspection=CandidateValidationError,
            residue=CandidateValidationError,
        ),
        legacy_format="owner-v1",
        legacy_owners=LEGACY_CANDIDATE_OWNERS,
        legacy_validated_has_contract_digest=True,
    )


def tree_digest(path: Path) -> str:
    try:
        return _shared_tree_digest(path)
    except MetadataError as error:
        raise CandidateValidationError("tree was unsafe") from error


def contract_digest(root: Path) -> str:
    try:
        return files_digest(root, CONTRACT_PATHS)
    except MetadataError as error:
        raise CandidateValidationError("candidate contract source was unsafe") from error


def run_validator(root: Path, *, scope_game: str | None = None) -> None:
    command = [sys.executable, "tools/validate_repository.py", "--root", str(root)]
    if scope_game:
        command.extend(("--scope-game", scope_game))
    try:
        result = subprocess.run(
            command,
            cwd=root,
            check=False,
            capture_output=True,
            timeout=command_timeout(90),
        )
    except subprocess.TimeoutExpired as error:
        raise CandidateValidationError("repository Validator timed out") from error
    if result.returncode:
        diagnostic = hashlib.sha256(result.stdout + b"\0" + result.stderr).hexdigest()[:12]
        raise CandidateValidationError(f"repository Validator failed diagnostic={diagnostic}")


def prevalidate_repository(repo_root: Path) -> None:
    try:
        required = (
            repo_root / "data/manifest.json",
            repo_root / "tools/validate_repository.py",
            repo_root / HSR_RELATIVE / "profile.json",
        )
        if not all(path.is_file() and not path.is_symlink() for path in required):
            raise OSError
        run_validator(repo_root)
    except (OSError, CandidateValidationError) as error:
        raise FormalRepositoryPrevalidationError(
            "formal repository prevalidation failed"
        ) from error


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
    base: Path

    @property
    def game_dir(self) -> Path:
        return self.root / HSR_RELATIVE

    def cleanup(self) -> None:
        if os.path.lexists(self.root):
            cleanup_candidate(self.root.name, candidate_base=self.base)


def canonical_business_digest(root: Path) -> str:
    profile, roster, builds = load_formal(root)
    return business_digest(snapshot_from_files(profile, roster, builds))


def create_candidate(
    repo_root: Path,
    snapshot: Snapshot,
    *,
    candidate_base: Path | None = None,
    fault_injector: Callable[[str], None] | None = None,
) -> Candidate:
    root = repo_root.resolve()
    prevalidate_repository(root)

    def populate(workspace: CandidateWorkspace) -> None:
        game = workspace.target_dir
        (game / "builds").mkdir(parents=True)
        try:
            copy_regular_file(
                root / HSR_RELATIVE / "builds/.gitkeep",
                game / "builds/.gitkeep",
            )
        except MetadataError as error:
            raise CandidateValidationError("candidate Build placeholder was unsafe") from error
        atomic_write_json(game / "profile.json", snapshot.profile)
        atomic_write_json(game / "roster.json", snapshot.roster)
        for character_id, build in snapshot.builds.items():
            atomic_write_json(game / "builds" / f"{character_id}.json", build)

    def validate(candidate_root: Path) -> None:
        run_validator(candidate_root, scope_game="honkai-star-rail")
        if snapshot.profile["sync"]["state"] != "partial":
            raise CandidateValidationError("candidate was not partial")

    def evidence(workspace: CandidateWorkspace) -> CandidateEvidence:
        repository_contract_digest = contract_digest(root)
        if contract_digest(workspace.root) != repository_contract_digest:
            raise CandidateValidationError("candidate contract copy drifted")
        return CandidateEvidence(
            game_tree_digest=tree_digest(workspace.target_dir),
            business_digest=canonical_business_digest(workspace.root),
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
