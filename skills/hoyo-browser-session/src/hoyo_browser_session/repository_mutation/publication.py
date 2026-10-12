"""Single-authority Git publication saga for repository data mutations."""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import stat
import sys
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from hoyo_browser_session.blocking import operation_deadline, run_blocking, run_cleanup
from hoyo_browser_session.repository_identity import (
    GitExecutionError,
    RepositoryIdentityError,
    git_output,
    git_succeeds,
    validate_repository_identity,
)

from .common import MetadataError, raise_as, require_real_directory

ORIGIN_MAIN_REF = "refs/remotes/origin/main"
REMOTE_MAIN_REF = "refs/heads/main"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

FaultInjector = Callable[[str], None]
SyncRunner = Callable[[Path, str], Awaitable[int]]


class GitRunner(Protocol):
    def __call__(
        self,
        repo_root: Path,
        *arguments: str,
        failure: type[Exception] | None = None,
        configuration: Mapping[str, str] | None = None,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class PublicationErrors:
    publish: type[Exception]
    configuration: type[Exception]
    path_closure: type[Exception]
    remote_moved: type[Exception]
    push_rejected: type[Exception]
    outcome_unknown: type[Exception]
    cleanup: type[Exception]


@dataclass(frozen=True, slots=True)
class PublicationPolicy:
    worktree_base: Path
    branch_prefix: str
    target_relative: Path
    transaction_root_relative: Path
    commit_message: str
    is_allowed_path: Callable[[str], bool]
    validate_business: Callable[[Path], None]
    assert_no_residue: Callable[[Path], None]
    prepare_worktree: Callable[[Path], None]
    errors: PublicationErrors

    def __post_init__(self) -> None:
        for path in (self.target_relative, self.transaction_root_relative):
            if path.is_absolute() or not path.parts or ".." in path.parts:
                raise ValueError("publication policy path was invalid")
        if not self.branch_prefix or not self.commit_message or "\n" in self.commit_message:
            raise ValueError("publication policy was invalid")


@dataclass(frozen=True, slots=True)
class PublishResult:
    no_op: bool
    commit_sha: str | None
    temporary_worktree_removed: bool
    temporary_branch_removed: bool


@dataclass(slots=True)
class TemporaryResources:
    run_root: Path
    worktree: Path
    branch: str
    expected_branch_sha: str
    branch_owned: bool = False
    worktree_added: bool = False


@dataclass(frozen=True, slots=True)
class PublicationOperations:
    repository_identity_root: Callable[[Path], Path]
    fetch_origin_main: Callable[[Path], None]
    push_origin_main: Callable[[Path, str], None]
    git: GitRunner
    create_resources: Callable[
        [Path, str, Path, FaultInjector | None],
        TemporaryResources,
    ]
    cleanup_resources: Callable[
        [Path, TemporaryResources, FaultInjector | None],
        None,
    ]


def _fault(injector: FaultInjector | None, stage: str) -> None:
    if injector is not None:
        injector(stage)


def default_git_runner(errors: PublicationErrors) -> GitRunner:
    def run(
        repo_root: Path,
        *arguments: str,
        failure: type[Exception] | None = None,
        configuration: Mapping[str, str] | None = None,
    ) -> str:
        try:
            return git_output(repo_root, *arguments, configuration=configuration)
        except GitExecutionError as error:
            raise_as(failure or errors.publish, "Git operation failed", error)

    return run


def repository_identity_root(
    policy: PublicationPolicy,
    path: Path,
) -> Path:
    try:
        return validate_repository_identity(path).root
    except RepositoryIdentityError as error:
        raise_as(
            policy.errors.configuration,
            "repository execution identity was rejected",
            error,
        )


def fetch_origin_main(
    policy: PublicationPolicy,
    repo_root: Path,
    git: GitRunner,
    *,
    identity_root: Callable[[Path], Path] | None = None,
) -> None:
    (identity_root or (lambda path: repository_identity_root(policy, path)))(repo_root)
    git(repo_root, "fetch", "--no-tags", "origin", "main")


def push_origin_main(
    policy: PublicationPolicy,
    repo_root: Path,
    data_commit_sha: str,
    git: GitRunner,
    *,
    identity_root: Callable[[Path], Path] | None = None,
) -> None:
    (identity_root or (lambda path: repository_identity_root(policy, path)))(repo_root)
    if SHA_RE.fullmatch(data_commit_sha) is None:
        raise_as(policy.errors.publish, "data commit SHA was invalid")
    parents = git(repo_root, "rev-list", "--parents", "-n", "1", data_commit_sha).split()
    if len(parents) != 2 or parents[0] != data_commit_sha or SHA_RE.fullmatch(parents[1]) is None:
        raise_as(policy.errors.publish, "data commit must have exactly one parent")
    expected_base_sha = parents[1]
    # Git sends the advertised old object ID to receive-pack, which checks it
    # atomically. The owned hook binds that advertisement to our expected base.
    guard = (
        "import sys\n"
        "updates = [line.split() for line in sys.stdin]\n"
        f"expected = [['{data_commit_sha}', 'refs/heads/main', '{expected_base_sha}']]\n"
        "observed = [parts[1:] for parts in updates if len(parts) == 4]\n"
        "sys.exit(0 if len(updates) == 1 and observed == expected else 1)\n"
    )
    with tempfile.TemporaryDirectory(prefix="hoyo-push-guard-") as temporary:
        directory = Path(temporary)
        hook = directory / "pre-push"
        hook.write_text(
            f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(guard)}\n",
            encoding="utf-8",
        )
        hook.chmod(0o700)
        git(
            repo_root,
            "push",
            "origin",
            f"{data_commit_sha}:{REMOTE_MAIN_REF}",
            failure=policy.errors.push_rejected,
            configuration={"core.hooksPath": str(directory)},
        )


def ref_sha(
    policy: PublicationPolicy,
    repo_root: Path,
    ref: str,
    git: GitRunner,
) -> str:
    value = git(repo_root, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if SHA_RE.fullmatch(value) is None:
        raise_as(
            policy.errors.publish,
            "Git ref did not resolve to an exact commit",
        )
    return value


def assert_remote_base(
    policy: PublicationPolicy,
    repo_root: Path,
    expected_base_sha: str,
    git: GitRunner,
) -> None:
    if ref_sha(policy, repo_root, ORIGIN_MAIN_REF, git) != expected_base_sha:
        raise_as(policy.errors.remote_moved, "origin/main moved during publish")


def secure_worktree_base(
    policy: PublicationPolicy,
    repo_root: Path,
    value: Path,
) -> Path:
    base = value.expanduser().absolute()
    try:
        if os.path.lexists(base):
            metadata = base.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise MetadataError("worktree base was unsafe")
        else:
            base.mkdir(parents=True, mode=0o700)
        base.chmod(0o700)
        metadata = require_real_directory(base)
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise MetadataError("worktree base mode was invalid")
        resolved = base.resolve(strict=True)
    except (MetadataError, OSError) as error:
        raise_as(
            policy.errors.configuration,
            "temporary worktree base could not be secured",
            error,
        )
    if resolved == repo_root or resolved.is_relative_to(repo_root):
        raise_as(
            policy.errors.configuration,
            "temporary worktree base must be outside the repository",
        )
    return resolved


def branch_exists(
    policy: PublicationPolicy,
    repo_root: Path,
    branch: str,
) -> bool:
    try:
        return git_succeeds(
            repo_root,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
        )
    except GitExecutionError as error:
        raise_as(policy.errors.publish, "Git operation failed", error)


def worktree_registered(
    repo_root: Path,
    worktree: Path,
    git: GitRunner,
) -> bool:
    listing = git(repo_root, "worktree", "list", "--porcelain")
    return f"worktree {worktree}" in listing.splitlines()


def create_temporary_resources(
    policy: PublicationPolicy,
    repo_root: Path,
    *,
    base_sha: str,
    worktree_base: Path,
    git: GitRunner,
    fault_injector: FaultInjector | None = None,
) -> TemporaryResources:
    token = str(uuid.uuid4())
    if str(uuid.UUID(token)) != token:
        raise_as(policy.errors.publish, "temporary publish identifier was invalid")
    run_root = worktree_base / token
    worktree = run_root / "worktree"
    branch = f"{policy.branch_prefix}{token}"
    if os.path.lexists(run_root) or branch_exists(policy, repo_root, branch):
        raise_as(policy.errors.publish, "temporary publish resources already exist")
    try:
        run_root.mkdir(mode=0o700)
        if stat.S_IMODE(require_real_directory(run_root).st_mode) != 0o700:
            raise MetadataError("temporary publish directory mode was invalid")
    except (MetadataError, OSError) as error:
        raise_as(
            policy.errors.publish,
            "temporary publish directory creation failed",
            error,
        )
    resources = TemporaryResources(
        run_root=run_root,
        worktree=worktree,
        branch=branch,
        expected_branch_sha=base_sha,
    )
    try:
        _fault(fault_injector, "publish_run_root_created")
        git(repo_root, "branch", branch, base_sha)
        resources.branch_owned = True
        _fault(fault_injector, "temporary_branch_created")
        git(repo_root, "worktree", "add", str(worktree), branch)
        resources.worktree_added = True
        _fault(fault_injector, "temporary_worktree_created")
        require_real_directory(worktree)
        if ref_sha(policy, worktree, "HEAD", git) != base_sha:
            raise_as(
                policy.errors.publish,
                "temporary worktree HEAD did not equal origin/main",
            )
        if ref_sha(policy, repo_root, f"refs/heads/{branch}", git) != base_sha:
            raise_as(
                policy.errors.publish,
                "temporary branch did not equal origin/main",
            )
    except BaseException as original_error:
        if not resources.worktree_added:
            resources.worktree_added = worktree_registered(
                repo_root,
                worktree,
                git,
            )
        try:
            cleanup_temporary_resources(
                policy,
                repo_root,
                resources,
                git=git,
                fault_injector=fault_injector,
            )
        except BaseException as cleanup_error:
            raise cleanup_error from original_error
        raise
    return resources


def cleanup_temporary_resources(
    policy: PublicationPolicy,
    repo_root: Path,
    resources: TemporaryResources,
    *,
    git: GitRunner,
    fault_injector: FaultInjector | None = None,
) -> None:
    transaction_root = resources.worktree / policy.transaction_root_relative
    if resources.worktree_added and os.path.lexists(transaction_root):
        raise_as(
            policy.errors.cleanup,
            "temporary worktree requires explicit transaction recovery",
        )
    if resources.worktree_added:
        _fault(fault_injector, "before_worktree_cleanup")
        git(
            repo_root,
            "worktree",
            "remove",
            "--force",
            str(resources.worktree),
            failure=policy.errors.cleanup,
        )
        resources.worktree_added = False
        _fault(fault_injector, "after_worktree_cleanup")
    if os.path.lexists(resources.worktree):
        raise_as(
            policy.errors.cleanup,
            "temporary worktree cleanup left residue",
        )
    if resources.branch_owned and branch_exists(
        policy,
        repo_root,
        resources.branch,
    ):
        branch_ref = f"refs/heads/{resources.branch}"
        if ref_sha(policy, repo_root, branch_ref, git) != resources.expected_branch_sha:
            raise_as(
                policy.errors.cleanup,
                "temporary branch ownership drifted",
            )
        _fault(fault_injector, "before_branch_cleanup")
        git(
            repo_root,
            "update-ref",
            "-d",
            branch_ref,
            resources.expected_branch_sha,
            failure=policy.errors.cleanup,
        )
        _fault(fault_injector, "after_branch_cleanup")
    if branch_exists(policy, repo_root, resources.branch):
        raise_as(policy.errors.cleanup, "temporary branch cleanup left residue")
    try:
        resources.run_root.rmdir()
    except OSError as error:
        raise_as(
            policy.errors.cleanup,
            "temporary publish directory cleanup failed",
            error,
        )
    if os.path.lexists(resources.run_root):
        raise_as(
            policy.errors.cleanup,
            "temporary publish directory cleanup left residue",
        )


def nul_paths(output: str) -> set[str]:
    return {value for value in output.split("\0") if value}


def unstaged_paths(repo_root: Path, git: GitRunner) -> set[str]:
    return nul_paths(git(repo_root, "diff", "--name-only", "-z"))


def staged_paths(repo_root: Path, git: GitRunner) -> set[str]:
    return nul_paths(git(repo_root, "diff", "--cached", "--name-only", "-z"))


def untracked_paths(repo_root: Path, git: GitRunner) -> set[str]:
    return nul_paths(git(repo_root, "ls-files", "--others", "--exclude-standard", "-z"))


def changed_paths(repo_root: Path, git: GitRunner) -> set[str]:
    return (
        unstaged_paths(repo_root, git)
        | staged_paths(repo_root, git)
        | untracked_paths(repo_root, git)
    )


def assert_path_closure(
    policy: PublicationPolicy,
    paths: set[str],
) -> None:
    if not paths or any(not policy.is_allowed_path(path) for path in paths):
        raise_as(
            policy.errors.path_closure,
            "publish changes escaped the game-owned data path closure",
        )


def assert_commit(
    policy: PublicationPolicy,
    repo_root: Path,
    *,
    commit_sha: str,
    base_sha: str,
    git: GitRunner,
) -> None:
    parents = git(
        repo_root,
        "rev-list",
        "--parents",
        "-n",
        "1",
        commit_sha,
    ).split()
    if parents != [commit_sha, base_sha]:
        raise_as(
            policy.errors.publish,
            "data commit parent did not equal initial origin/main",
        )
    if git(repo_root, "show", "-s", "--format=%B", commit_sha) != (policy.commit_message):
        raise_as(policy.errors.publish, "data commit message was not fixed")
    committed_paths = nul_paths(
        git(
            repo_root,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "-z",
            commit_sha,
        )
    )
    assert_path_closure(policy, committed_paths)


def default_operations(policy: PublicationPolicy) -> PublicationOperations:
    git = default_git_runner(policy.errors)
    return PublicationOperations(
        repository_identity_root=lambda path: repository_identity_root(policy, path),
        fetch_origin_main=lambda root: fetch_origin_main(policy, root, git),
        push_origin_main=lambda root, sha: push_origin_main(
            policy,
            root,
            sha,
            git,
        ),
        git=git,
        create_resources=lambda root, sha, base, fault: (
            create_temporary_resources(
                policy,
                root,
                base_sha=sha,
                worktree_base=base,
                git=git,
                fault_injector=fault,
            )
        ),
        cleanup_resources=lambda root, resources, fault: (
            cleanup_temporary_resources(
                policy,
                root,
                resources,
                git=git,
                fault_injector=fault,
            )
        ),
    )


def bind_operations(
    *,
    identity_root: Callable[[Path], Path],
    fetch: Callable[[Path], None],
    push: Callable[[Path, str], None],
    git: GitRunner,
    create: Callable[..., TemporaryResources],
    cleanup: Callable[..., None],
) -> PublicationOperations:
    """Bind game facades once while preserving their owned-resource hooks."""

    return PublicationOperations(
        repository_identity_root=identity_root,
        fetch_origin_main=fetch,
        push_origin_main=push,
        git=git,
        create_resources=lambda root, sha, base, fault: create(
            root,
            base_sha=sha,
            worktree_base=base,
            fault_injector=fault,
        ),
        cleanup_resources=lambda root, resources, fault: (
            cleanup(root, resources)
            if fault is None
            else cleanup(root, resources, fault_injector=fault)
        ),
    )


async def publish_main(
    policy: PublicationPolicy,
    repo_root: Path,
    *,
    sync_runner: SyncRunner,
    worktree_base: Path | None = None,
    operations: PublicationOperations | None = None,
    fault_injector: FaultInjector | None = None,
) -> PublishResult:
    """Run one Apply in a disposable worktree and CAS-push its data commit."""
    ops = operations or default_operations(policy)
    repository = await run_blocking(ops.repository_identity_root, repo_root)
    await run_blocking(ops.fetch_origin_main, repository)
    base_sha = await run_blocking(ref_sha, policy, repository, ORIGIN_MAIN_REF, ops.git)
    secured_base = await run_blocking(
        secure_worktree_base, policy, repository, worktree_base or policy.worktree_base
    )

    def cleanup_cancelled_resources(value: TemporaryResources) -> None:
        with operation_deadline(60):
            ops.cleanup_resources(repository, value, fault_injector)

    resources = await run_blocking(
        ops.create_resources,
        repository,
        base_sha,
        secured_base,
        fault_injector,
        on_cancel=cleanup_cancelled_resources,
    )
    result: PublishResult | None = None
    data_commit_sha: str | None = None
    cleanup_attempted = False
    try:
        sync_result = await sync_runner(resources.worktree, base_sha)
        if sync_result != 0:
            raise_as(policy.errors.publish, "synchronization returned a nonzero status")
        await run_blocking(policy.assert_no_residue, resources.worktree)
        await run_blocking(policy.prepare_worktree, resources.worktree)
        paths = await run_blocking(changed_paths, resources.worktree, ops.git)
        if not paths:
            _fault(fault_injector, "before_remote_main_cas")
            await run_blocking(ops.fetch_origin_main, repository)
            await run_blocking(assert_remote_base, policy, repository, base_sha, ops.git)
            cleanup_attempted = True
            await run_cleanup(ops.cleanup_resources, repository, resources, fault_injector)
            result = PublishResult(True, None, True, True)
        else:
            await run_blocking(assert_path_closure, policy, paths)
            await run_blocking(policy.validate_business, resources.worktree)
            await run_blocking(ops.git, resources.worktree, "diff", "--check")
            _fault(fault_injector, "before_remote_main_cas")
            await run_blocking(ops.fetch_origin_main, repository)
            await run_blocking(assert_remote_base, policy, repository, base_sha, ops.git)
            await run_blocking(
                ops.git, resources.worktree, "add", "--", policy.target_relative.as_posix()
            )
            await run_blocking(
                assert_path_closure,
                policy,
                await run_blocking(staged_paths, resources.worktree, ops.git),
            )
            if await run_blocking(
                unstaged_paths, resources.worktree, ops.git
            ) or await run_blocking(untracked_paths, resources.worktree, ops.git):
                raise_as(
                    policy.errors.path_closure,
                    "temporary worktree remained dirty after data-only staging",
                )
            await run_blocking(policy.assert_no_residue, resources.worktree)
            await run_blocking(
                ops.git,
                resources.worktree,
                "-c",
                "commit.gpgSign=false",
                "commit",
                "--no-verify",
                "--no-gpg-sign",
                "-m",
                policy.commit_message,
            )
            commit_sha = await run_blocking(ref_sha, policy, resources.worktree, "HEAD", ops.git)
            resources.expected_branch_sha = commit_sha
            _fault(fault_injector, "commit_created")
            await run_blocking(
                assert_commit,
                policy,
                resources.worktree,
                commit_sha=commit_sha,
                base_sha=base_sha,
                git=ops.git,
            )
            if await run_blocking(changed_paths, resources.worktree, ops.git):
                raise_as(
                    policy.errors.path_closure, "temporary worktree was dirty after data commit"
                )
            await run_blocking(policy.assert_no_residue, resources.worktree)
            cleanup_attempted = True
            await run_cleanup(ops.cleanup_resources, repository, resources, fault_injector)
            data_commit_sha = commit_sha
    except BaseException as original_error:
        if not cleanup_attempted:
            cleanup_attempted = True
            try:
                await run_cleanup(ops.cleanup_resources, repository, resources, fault_injector)
            except BaseException as cleanup_error:
                raise cleanup_error from original_error
        raise
    if result is not None:
        return result
    if data_commit_sha is None:
        raise_as(policy.errors.publish, "publish did not produce a result")
    await run_blocking(ops.fetch_origin_main, repository)
    await run_blocking(assert_remote_base, policy, repository, base_sha, ops.git)
    if await run_blocking(ref_sha, policy, repository, data_commit_sha, ops.git) != data_commit_sha:
        raise_as(policy.errors.publish, "data commit object did not resolve exactly")
    _fault(fault_injector, "before_push")
    push_error: BaseException | None = None
    try:
        await run_blocking(ops.push_origin_main, repository, data_commit_sha)
        _fault(fault_injector, "after_push")
    except asyncio.CancelledError as error:
        raise_as(policy.errors.outcome_unknown, "publish was interrupted during push", error)
    except BaseException as error:
        push_error = error
    try:
        await run_blocking(ops.fetch_origin_main, repository)
        remote_sha = await run_blocking(ref_sha, policy, repository, ORIGIN_MAIN_REF, ops.git)
    except BaseException as error:
        raise_as(policy.errors.outcome_unknown, "publish outcome requires reconciliation", error)
    if remote_sha == data_commit_sha:
        return PublishResult(False, data_commit_sha, True, True)
    if remote_sha == base_sha:
        raise_as(
            policy.errors.push_rejected,
            "remote main proved that the data commit was not accepted",
            push_error,
        )
    raise_as(
        policy.errors.remote_moved,
        "origin/main moved to an unexpected commit during push reconciliation",
        push_error,
    )
