"""HSR publication policy over the shared Git saga."""

from __future__ import annotations

import os
import re
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path, PurePosixPath

from hoyo_browser_session.errors import PublishOutcomeUnknown
from hoyo_browser_session.repository_identity import (
    GitExecutionError,
    RepositoryIdentityError,
    canonical_repository_identity,
    git_output,
    validate_repository_identity,
    validate_repository_root,
)
from hoyo_browser_session.repository_mutation import publication as mutation

from .candidate import assert_no_stale_candidates
from .errors import (
    DirectMainPushRejected,
    PublishCleanupError,
    PublishConfigurationError,
    PublishError,
    PublishPathClosureError,
    RemoteMainMoved,
)
from .jsonio import StrictJsonError, load_json, require_real_directory, require_regular_file

ORIGIN_MAIN_REF = mutation.ORIGIN_MAIN_REF
REMOTE_MAIN_REF = mutation.REMOTE_MAIN_REF
COMMIT_MESSAGE = "data(hsr): refresh account state"
DEFAULT_WORKTREE_BASE = Path("~/.local/share/hoyo-game-data/worktrees/hsr-direct-publish")
TEMPORARY_BRANCH_PREFIX = "hsr-direct-publish/"
HSR_ROOT = PurePosixPath("data/honkai-star-rail")
HSR_TOP_LEVEL_FILES = frozenset(
    {
        "data/honkai-star-rail/profile.json",
        "data/honkai-star-rail/roster.json",
    }
)
BUILD_PLACEHOLDER = Path("data/honkai-star-rail/builds/.gitkeep")
BUILD_NAME_RE = re.compile(r"^[1-9][0-9]*\.json$")

SyncRunner = Callable[[Path, str], Awaitable[int]]
PublishResult = mutation.PublishResult
_TemporaryResources = mutation.TemporaryResources


def _errors() -> mutation.PublicationErrors:
    return mutation.PublicationErrors(
        publish=PublishError,
        configuration=PublishConfigurationError,
        path_closure=PublishPathClosureError,
        remote_moved=RemoteMainMoved,
        push_rejected=DirectMainPushRejected,
        outcome_unknown=PublishOutcomeUnknown,
        cleanup=PublishCleanupError,
    )


def _policy() -> mutation.PublicationPolicy:
    return mutation.PublicationPolicy(
        worktree_base=DEFAULT_WORKTREE_BASE,
        branch_prefix=TEMPORARY_BRANCH_PREFIX,
        target_relative=Path("data/honkai-star-rail"),
        transaction_root_relative=Path(".hsr-sync-transactions"),
        commit_message=COMMIT_MESSAGE,
        is_allowed_path=_is_allowed_hsr_path,
        validate_business=_assert_roster_build_bijection,
        assert_no_residue=_assert_no_publish_residue,
        prepare_worktree=_restore_tracked_build_placeholder,
        errors=_errors(),
    )


def _git(
    repo_root: Path,
    *arguments: str,
    failure: type[Exception] | None = None,
    configuration: Mapping[str, str] | None = None,
) -> str:
    try:
        return git_output(repo_root, *arguments, configuration=configuration)
    except GitExecutionError as error:
        raise (failure or PublishError)("Git operation failed") from error


def _validate_origin_url(url: str) -> None:
    try:
        canonical_repository_identity(url)
    except RepositoryIdentityError as error:
        raise PublishConfigurationError("origin must identify WEX025/hoyo-game-data") from error


def _repository_root(path: Path) -> Path:
    try:
        return validate_repository_root(path)
    except RepositoryIdentityError as error:
        raise PublishConfigurationError(
            "repo-root does not contain the HSR Skill contract"
        ) from error


def _repository_identity_root(path: Path) -> Path:
    try:
        return validate_repository_identity(path).root
    except RepositoryIdentityError as error:
        raise PublishConfigurationError("repository execution identity was rejected") from error


def _fetch_origin_main(repo_root: Path) -> None:
    mutation.fetch_origin_main(
        _policy(),
        repo_root,
        _git,
        identity_root=_repository_identity_root,
    )


def _push_origin_main(repo_root: Path, data_commit_sha: str) -> None:
    mutation.push_origin_main(
        _policy(),
        repo_root,
        data_commit_sha,
        _git,
        identity_root=_repository_identity_root,
    )


def _ref_sha(repo_root: Path, ref: str) -> str:
    return mutation.ref_sha(_policy(), repo_root, ref, _git)


def _assert_remote_base(repo_root: Path, expected_base_sha: str) -> None:
    mutation.assert_remote_base(
        _policy(),
        repo_root,
        expected_base_sha,
        _git,
    )


def _secure_worktree_base(repo_root: Path, worktree_base: Path) -> Path:
    return mutation.secure_worktree_base(_policy(), repo_root, worktree_base)


def _branch_exists(repo_root: Path, branch: str) -> bool:
    return mutation.branch_exists(_policy(), repo_root, branch)


def _worktree_registered(repo_root: Path, worktree: Path) -> bool:
    return mutation.worktree_registered(repo_root, worktree, _git)


def _create_temporary_resources(
    repo_root: Path,
    *,
    base_sha: str,
    worktree_base: Path,
    fault_injector: mutation.FaultInjector | None = None,
) -> _TemporaryResources:
    return mutation.create_temporary_resources(
        _policy(),
        repo_root,
        base_sha=base_sha,
        worktree_base=worktree_base,
        git=_git,
        fault_injector=fault_injector,
    )


def _cleanup_temporary_resources(
    repo_root: Path,
    resources: _TemporaryResources,
    *,
    fault_injector: mutation.FaultInjector | None = None,
) -> None:
    mutation.cleanup_temporary_resources(
        _policy(),
        repo_root,
        resources,
        git=_git,
        fault_injector=fault_injector,
    )


def _nul_paths(output: str) -> set[str]:
    return mutation.nul_paths(output)


def _unstaged_paths(repo_root: Path) -> set[str]:
    return mutation.unstaged_paths(repo_root, _git)


def _staged_paths(repo_root: Path) -> set[str]:
    return mutation.staged_paths(repo_root, _git)


def _untracked_paths(repo_root: Path) -> set[str]:
    return mutation.untracked_paths(repo_root, _git)


def _changed_paths(repo_root: Path) -> set[str]:
    return mutation.changed_paths(repo_root, _git)


def _restore_tracked_build_placeholder(repo_root: Path) -> None:
    relative = BUILD_PLACEHOLDER.as_posix()
    tracked = _git(
        repo_root,
        "ls-tree",
        "--name-only",
        "HEAD",
        "--",
        relative,
    )
    if not tracked:
        return
    if tracked != relative or _git(repo_root, "show", f"HEAD:{relative}"):
        raise PublishPathClosureError("tracked HSR Build placeholder was invalid")
    path = repo_root / BUILD_PLACEHOLDER
    if os.path.lexists(path):
        try:
            require_regular_file(path)
            if path.read_bytes():
                raise OSError
        except (OSError, StrictJsonError) as error:
            raise PublishPathClosureError("HSR Build placeholder was unsafe") from error
        return
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        os.close(descriptor)
    except OSError as error:
        raise PublishPathClosureError("HSR Build placeholder could not be restored") from error


def _is_allowed_hsr_path(path: str) -> bool:
    if path in HSR_TOP_LEVEL_FILES:
        return True
    pure = PurePosixPath(path)
    return pure.parent == HSR_ROOT / "builds" and BUILD_NAME_RE.fullmatch(pure.name) is not None


def _assert_data_only_path_closure(paths: set[str]) -> None:
    mutation.assert_path_closure(_policy(), paths)


def _assert_roster_build_bijection(repo_root: Path) -> None:
    try:
        roster = load_json(repo_root / "data/honkai-star-rail/roster.json")
        if not isinstance(roster, dict):
            raise ValueError
        characters = roster.get("characters")
        if not isinstance(characters, dict) or any(
            not isinstance(identifier, str) or BUILD_NAME_RE.fullmatch(f"{identifier}.json") is None
            for identifier in characters
        ):
            raise ValueError
        builds = repo_root / "data/honkai-star-rail/builds"
        require_real_directory(builds)
        build_ids: set[str] = set()
        for path in builds.iterdir():
            if path.name == ".gitkeep":
                require_regular_file(path)
                continue
            if BUILD_NAME_RE.fullmatch(path.name) is None:
                raise ValueError
            require_regular_file(path)
            build_ids.add(path.stem)
        if set(characters) != build_ids:
            raise ValueError
    except (OSError, StrictJsonError, ValueError) as error:
        raise PublishPathClosureError(
            "HSR roster and Build identities are not bijective"
        ) from error


def _assert_no_publish_residue(repo_root: Path) -> None:
    assert_no_stale_candidates()
    if os.path.lexists(repo_root / ".hsr-sync-transactions"):
        raise PublishError("Apply transaction residue remains after synchronization")


def _assert_commit(
    repo_root: Path,
    *,
    commit_sha: str,
    base_sha: str,
) -> None:
    mutation.assert_commit(
        _policy(),
        repo_root,
        commit_sha=commit_sha,
        base_sha=base_sha,
        git=_git,
    )


def _operations() -> mutation.PublicationOperations:
    return mutation.bind_operations(
        identity_root=_repository_identity_root,
        fetch=_fetch_origin_main,
        push=_push_origin_main,
        git=_git,
        create=_create_temporary_resources,
        cleanup=_cleanup_temporary_resources,
    )


async def publish_main(
    repo_root: Path,
    *,
    sync_runner: SyncRunner,
    worktree_base: Path = DEFAULT_WORKTREE_BASE,
    fault_injector: mutation.FaultInjector | None = None,
) -> PublishResult:
    result = await mutation.publish_main(
        _policy(),
        repo_root,
        sync_runner=sync_runner,
        worktree_base=worktree_base,
        operations=_operations(),
        fault_injector=fault_injector,
    )
    return result
