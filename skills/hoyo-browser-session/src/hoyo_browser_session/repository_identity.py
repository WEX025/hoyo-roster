"""Canonical repository and Broker execution identity authority."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import SplitResult, urlsplit

from .blocking import bounded_command, command_timeout

LOGICAL_REPOSITORY_IDENTITY = "github.com/WEX025/hoyo-game-data"
REPOSITORY_CONTRACT_PATHS = (
    "AGENTS.md",
    "data/manifest.json",
    "schemas/repository-manifest.schema.json",
    "tools/validate_repository.py",
    "skills/hoyo-browser-session/pyproject.toml",
    "skills/genshin-character-state-sync/pyproject.toml",
    "skills/zzz-character-state-sync/pyproject.toml",
    "skills/hsr-character-state-sync/pyproject.toml",
)

_EXPECTED_HOST = "github.com"
_EXPECTED_OWNER = "WEX025"
_EXPECTED_NAME = "hoyo-game-data"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY_TOKEN_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_EXECUTION_KEY_V2_RE = re.compile(
    r"^repository-execution/v2:"
    r"(?P<runtime_digest>[0-9a-f]{64}):"
    r"(?P<contract_version>[1-9][0-9]*):"
    r"(?P<identity_digest>[0-9a-f]{64})$"
)
_REPOSITORY_BINDING_ENVIRONMENT = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_NAMESPACE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
        "GIT_QUARANTINE_PATH",
        "GIT_WORK_TREE",
    }
)


class RepositoryIdentityError(ValueError):
    """A local path or Git remote did not prove the fixed repository identity."""


class GitExecutionError(RuntimeError):
    """A fixed Git command could not execute under the safe repository context."""


@dataclass(frozen=True, slots=True)
class RepositoryIdentity:
    logical_identity: str
    root: Path = field(repr=False)

    def __post_init__(self) -> None:
        if self.logical_identity != LOGICAL_REPOSITORY_IDENTITY:
            raise ValueError("logical repository identity was invalid")
        if not self.root.is_absolute():
            raise ValueError("repository execution root was not absolute")


@dataclass(frozen=True, slots=True)
class RepositoryExecutionIdentity:
    repository: str
    account_scope: str
    game: str
    operation: str
    runtime_digest: str
    operation_contract_version: int

    def __post_init__(self) -> None:
        if self.repository != LOGICAL_REPOSITORY_IDENTITY:
            raise ValueError("execution repository identity was invalid")
        for label, value in (
            ("account scope", self.account_scope),
            ("game", self.game),
            ("operation", self.operation),
        ):
            if _IDENTITY_TOKEN_RE.fullmatch(value) is None:
                raise ValueError(f"execution {label} was invalid")
        if _DIGEST_RE.fullmatch(self.runtime_digest) is None:
            raise ValueError("execution Runtime digest was invalid")
        if type(self.operation_contract_version) is not int or self.operation_contract_version < 1:
            raise ValueError("execution operation contract version was invalid")

    @property
    def dedupe_key(self) -> str:
        document = json.dumps(
            {
                "account_scope": self.account_scope,
                "game": self.game,
                "operation": self.operation,
                "operation_contract_version": self.operation_contract_version,
                "repository": self.repository,
                "runtime_digest": self.runtime_digest,
                "schema_version": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        identity_digest = hashlib.sha256(document).hexdigest()
        return (
            f"repository-execution/v2:{self.runtime_digest}:"
            f"{self.operation_contract_version}:{identity_digest}"
        )


def repository_execution_generation(dedupe_key: str) -> tuple[str, int] | None:
    match = _EXECUTION_KEY_V2_RE.fullmatch(dedupe_key)
    if match is None:
        return None
    return (
        match.group("runtime_digest"),
        int(match.group("contract_version")),
    )


def _canonical_path(path: str) -> bool:
    if not path or "\\" in path or "%" in path:
        return False
    normalized = path
    if normalized.endswith("/"):
        normalized = normalized[:-1]
    if not normalized or normalized.endswith("/"):
        return False
    if normalized.endswith(".git"):
        normalized = normalized.removesuffix(".git")
    parts = normalized.removeprefix("/").split("/")
    return (
        len(parts) == 2
        and parts[0].casefold() == _EXPECTED_OWNER.casefold()
        and parts[1].casefold() == _EXPECTED_NAME.casefold()
    )


def _canonical_url_identity(parsed: SplitResult) -> str:
    try:
        port = parsed.port
    except ValueError as error:
        raise RepositoryIdentityError("repository Origin identity was invalid") from error
    if parsed.query or parsed.fragment or port is not None or not _canonical_path(parsed.path):
        raise RepositoryIdentityError("repository Origin identity was invalid")
    https = (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.hostname.casefold() == _EXPECTED_HOST
        and parsed.username is None
        and parsed.password is None
        and parsed.netloc.casefold() == _EXPECTED_HOST
    )
    ssh = (
        parsed.scheme == "ssh"
        and parsed.hostname is not None
        and parsed.hostname.casefold() == _EXPECTED_HOST
        and parsed.username == "git"
        and parsed.password is None
        and parsed.netloc.casefold() == f"git@{_EXPECTED_HOST}"
    )
    if not (https or ssh):
        raise RepositoryIdentityError("repository Origin identity was invalid")
    return LOGICAL_REPOSITORY_IDENTITY


def canonical_repository_identity(origin_url: str) -> str:
    """Normalize only exact credential-free HTTPS and GitHub SSH forms."""

    if (
        type(origin_url) is not str
        or not origin_url
        or origin_url != origin_url.strip()
        or any(ord(character) < 0x20 for character in origin_url)
    ):
        raise RepositoryIdentityError("repository Origin identity was invalid")
    scp = re.fullmatch(r"git@([^/:?#]+):([^?#]+)", origin_url)
    if scp is not None:
        if scp.group(1).casefold() != _EXPECTED_HOST or not _canonical_path(scp.group(2)):
            raise RepositoryIdentityError("repository Origin identity was invalid")
        return LOGICAL_REPOSITORY_IDENTITY
    try:
        parsed = urlsplit(origin_url)
    except ValueError as error:
        raise RepositoryIdentityError("repository Origin identity was invalid") from error
    return _canonical_url_identity(parsed)


def git_execution_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Preserve authentication/config while removing repository-binding overrides."""

    values = os.environ if source is None else source
    return {
        key: value for key, value in values.items() if key not in _REPOSITORY_BINDING_ENVIRONMENT
    }


def git_output(
    repo_root: Path,
    *arguments: str,
    environment: Mapping[str, str] | None = None,
    acceptable_returncodes: Collection[int] = (0,),
    configuration: Mapping[str, str] | None = None,
) -> str:
    options = [
        option
        for key, value in (configuration or {}).items()
        for option in ("-c", f"{key}={value}")
    ]
    try:
        result = bounded_command(
            ["git", *options, *arguments],
            cwd=repo_root,
            env=git_execution_environment(environment),
            timeout=command_timeout(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GitExecutionError("required Git command could not be executed") from error
    if result.returncode not in acceptable_returncodes:
        raise GitExecutionError("required Git command failed")
    return result.stdout.decode("utf-8").rstrip("\n")


def git_bytes(
    repo_root: Path,
    *arguments: str,
    environment: Mapping[str, str] | None = None,
) -> bytes:
    try:
        result = bounded_command(
            ["git", *arguments],
            cwd=repo_root,
            env=git_execution_environment(environment),
            timeout=command_timeout(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GitExecutionError("required Git command could not be executed") from error
    if result.returncode != 0:
        raise GitExecutionError("required Git command failed")
    return result.stdout


def git_succeeds(
    repo_root: Path,
    *arguments: str,
    environment: Mapping[str, str] | None = None,
) -> bool:
    try:
        result = bounded_command(
            ["git", *arguments],
            cwd=repo_root,
            env=git_execution_environment(environment),
            timeout=command_timeout(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GitExecutionError("required Git command could not be executed") from error
    return result.returncode == 0


def _identity_git_output(repo_root: Path, *arguments: str) -> str:
    try:
        return git_output(repo_root, *arguments)
    except GitExecutionError as error:
        raise RepositoryIdentityError("repository Git identity was unavailable") from error


def _config_values(repo_root: Path, key: str) -> tuple[str, ...]:
    try:
        output = git_output(
            repo_root,
            "config",
            "--local",
            "--get-all",
            key,
            acceptable_returncodes=(0, 1),
        )
    except GitExecutionError as error:
        raise RepositoryIdentityError("repository Git config was unavailable") from error
    values = tuple(output.splitlines()) if output else ()
    if any(not value for value in values):
        raise RepositoryIdentityError("repository Git config was invalid")
    return values


def _remote_values(repo_root: Path, *, push: bool) -> tuple[str, ...]:
    arguments = ["remote", "get-url"]
    if push:
        arguments.append("--push")
    arguments.extend(("--all", "origin"))
    try:
        output = git_output(repo_root, *arguments)
    except GitExecutionError as error:
        raise RepositoryIdentityError("repository effective Origin was unavailable") from error
    values = tuple(output.splitlines()) if output else ()
    if len(values) != 1 or not values[0]:
        raise RepositoryIdentityError("repository effective Origin was ambiguous")
    return values


def _validate_registered_worktree(root: Path) -> None:
    dot_git = root / ".git"
    try:
        metadata = dot_git.lstat()
    except OSError as error:
        raise RepositoryIdentityError("repository Git topology was unavailable") from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode))
    ):
        raise RepositoryIdentityError("repository Git topology was unsafe")
    if _identity_git_output(root, "rev-parse", "--is-inside-work-tree") != "true":
        raise RepositoryIdentityError("repository worktree authority was invalid")
    listing = _identity_git_output(root, "worktree", "list", "--porcelain", "-z")
    matches = 0
    for record in listing.split("\0\0"):
        first = record.split("\0", 1)[0]
        if not first.startswith("worktree "):
            continue
        try:
            worktree = Path(first.removeprefix("worktree ")).resolve(strict=True)
        except OSError:
            continue
        if worktree == root:
            matches += 1
    if matches != 1:
        raise RepositoryIdentityError("repository worktree was not registered")


def _validate_contract_paths(root: Path, required_paths: Sequence[str]) -> None:
    for value in required_paths:
        relative = Path(value)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise RepositoryIdentityError("repository contract path was invalid")
        candidate = root / relative
        try:
            metadata = candidate.lstat()
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise RepositoryIdentityError("repository contract was unavailable") from error
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or resolved != candidate
        ):
            raise RepositoryIdentityError("repository contract was unsafe")


def validate_repository_root(
    repo_root: Path,
    *,
    required_paths: Sequence[str] = REPOSITORY_CONTRACT_PATHS,
) -> Path:
    """Require a canonical current-user Git top-level and regular contract files."""

    root = repo_root.expanduser().absolute()
    try:
        metadata = root.lstat()
        resolved = root.resolve(strict=True)
    except OSError as error:
        raise RepositoryIdentityError("repository root was unavailable") from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or resolved != root
    ):
        raise RepositoryIdentityError("repository root was unsafe")
    top_level_text = _identity_git_output(resolved, "rev-parse", "--show-toplevel")
    try:
        top_level = Path(top_level_text).resolve(strict=True)
    except OSError as error:
        raise RepositoryIdentityError("repository Git top-level was unavailable") from error
    if top_level != resolved:
        raise RepositoryIdentityError("repository root was not the Git top-level")
    _validate_registered_worktree(resolved)
    _validate_contract_paths(resolved, required_paths)
    return resolved


def validate_repository_identity(
    repo_root: Path,
    *,
    required_paths: Sequence[str] = REPOSITORY_CONTRACT_PATHS,
) -> RepositoryIdentity:
    """Prove the fixed logical repository identity without contacting a remote."""

    root = validate_repository_root(repo_root, required_paths=required_paths)
    origins = _config_values(root, "remote.origin.url")
    if len(origins) != 1:
        raise RepositoryIdentityError("repository Origin authority was ambiguous")
    logical_identity = canonical_repository_identity(origins[0])
    push_urls = _config_values(root, "remote.origin.pushurl")
    if len(push_urls) > 1:
        raise RepositoryIdentityError("repository push Origin authority was ambiguous")
    if push_urls:
        canonical_repository_identity(push_urls[0])
    effective_fetch = _remote_values(root, push=False)
    effective_push = _remote_values(root, push=True)
    if canonical_repository_identity(effective_fetch[0]) != logical_identity:
        raise RepositoryIdentityError("repository effective fetch identity changed")
    if canonical_repository_identity(effective_push[0]) != logical_identity:
        raise RepositoryIdentityError("repository effective push identity changed")
    return RepositoryIdentity(logical_identity=logical_identity, root=root)
