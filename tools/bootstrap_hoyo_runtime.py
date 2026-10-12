#!/usr/bin/env python3
"""Build and atomically activate the shared Hoyo Runtime from one exact Git ref."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from contextvars import ContextVar
from email.message import Message
from email.parser import BytesParser
from enum import StrEnum
from pathlib import Path
from typing import Any

_TOOLS_ROOT = Path(__file__).resolve().parent
if str(_TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TOOLS_ROOT))

from dependency_authority import (  # noqa: E402
    AUTHORITY_FILENAME,
    AuthorityError,
    DependencyAuthority,
    PackageMetadata,
    canonical_bytes,
    expected_installed_versions,
    parse_authority,
    validate_distribution_document,
    validate_package_metadata,
)

_SHARED_SOURCE_ROOT = (
    Path(__file__).resolve().parents[1] / "skills/hoyo-browser-session/src"
)
if str(_SHARED_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_SOURCE_ROOT))

from hoyo_browser_session.blocking import bounded_command  # noqa: E402
from hoyo_browser_session.repository_identity import (  # noqa: E402
    GitExecutionError,
    RepositoryIdentityError,
    canonical_repository_identity,
    git_bytes,
    git_output,
    git_succeeds,
    validate_repository_identity,
)
from hoyo_browser_session.repository_identity import (  # noqa: E402
    git_execution_environment as _git_execution_environment,
)

DEFAULT_EXTERNAL_ROOT = Path("~/.local/share/hoyo-game-data")
PLAYWRIGHT_BROWSERS_RELATIVE = Path("browser/playwright-browsers")
RUNTIME_RELATIVE = Path("runtimes/hoyo-sync")
SKILL_PATHS = (
    "skills/hoyo-browser-session",
    "skills/genshin-character-state-sync",
    "skills/zzz-character-state-sync",
    "skills/hsr-character-state-sync",
)
PACKAGE_MODULES = {
    "skills/hoyo-browser-session": "hoyo_browser_session",
    "skills/genshin-character-state-sync": "genshin_character_state_sync",
    "skills/zzz-character-state-sync": "zzz_character_state_sync",
    "skills/hsr-character-state-sync": "hsr_character_state_sync",
}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
ACTIVE_KEYS = frozenset(
    {
        "schema_version",
        "runtime_digest",
        "source_commit",
        "python_minor",
        "playwright_version",
        "platform",
        "architecture",
        "created_at",
    }
)


class LauncherErrorCode(StrEnum):
    INTERRUPTED = "INTERRUPTED"
    REPOSITORY_IDENTITY_INVALID = "REPOSITORY_IDENTITY_INVALID"
    REMOTE_FETCH_FAILED = "REMOTE_FETCH_FAILED"
    RUNTIME_METADATA_INVALID = "RUNTIME_METADATA_INVALID"
    RUNTIME_BUILD_FAILED = "RUNTIME_BUILD_FAILED"
    RUNTIME_ACTIVATION_FAILED = "RUNTIME_ACTIVATION_FAILED"
    CHILD_EXECUTION_FAILED = "CHILD_EXECUTION_FAILED"


class BootstrapError(Exception):
    code = LauncherErrorCode.RUNTIME_BUILD_FAILED.value


_BUILD_STAGE: ContextVar[str] = ContextVar("hoyo_build_stage", default="HOST_COMMAND")


class BootstrapStageError(BootstrapError):
    def __init__(self, stage: str, reason: str, diagnostic: str) -> None:
        super().__init__("Runtime build phase failed")
        self.stage = stage
        self.reason = reason
        self.diagnostic_sha256 = hashlib.sha256(diagnostic.encode()).hexdigest()


@contextlib.contextmanager
def _build_stage(stage: str) -> Iterator[None]:
    token = _BUILD_STAGE.set(stage)
    try:
        yield
    except BootstrapStageError:
        raise
    except BootstrapError as error:
        raise BootstrapStageError(stage, "VERIFICATION_FAILED", str(error)) from error
    finally:
        _BUILD_STAGE.reset(token)


def print_launcher_diagnostics(error: BaseException) -> None:
    if isinstance(error, BootstrapStageError):
        print(f"ERROR_STAGE={error.stage}", file=sys.stderr)
        print(f"ERROR_REASON={error.reason}", file=sys.stderr)
        print(f"ERROR_DIAGNOSTIC_SHA256={error.diagnostic_sha256}", file=sys.stderr)


class RepositoryIdentityInvalid(BootstrapError):
    code = LauncherErrorCode.REPOSITORY_IDENTITY_INVALID.value


class RemoteFetchFailed(BootstrapError):
    code = LauncherErrorCode.REMOTE_FETCH_FAILED.value


class RuntimeMetadataInvalid(BootstrapError):
    code = LauncherErrorCode.RUNTIME_METADATA_INVALID.value


class RuntimeActivationFailed(BootstrapError):
    code = LauncherErrorCode.RUNTIME_ACTIVATION_FAILED.value


class ChildExecutionFailed(BootstrapError):
    code = LauncherErrorCode.CHILD_EXECUTION_FAILED.value


def classify_launcher_error(
    error: BaseException,
    *,
    fallback: LauncherErrorCode,
) -> LauncherErrorCode:
    """Map exceptions to the fixed public launcher code set."""

    if isinstance(error, RepositoryIdentityInvalid):
        return LauncherErrorCode.REPOSITORY_IDENTITY_INVALID
    if isinstance(error, RemoteFetchFailed):
        return LauncherErrorCode.REMOTE_FETCH_FAILED
    if isinstance(error, RuntimeMetadataInvalid):
        return LauncherErrorCode.RUNTIME_METADATA_INVALID
    if isinstance(error, RuntimeActivationFailed):
        return LauncherErrorCode.RUNTIME_ACTIVATION_FAILED
    if isinstance(error, ChildExecutionFailed):
        return LauncherErrorCode.CHILD_EXECUTION_FAILED
    if isinstance(error, BootstrapError):
        return LauncherErrorCode.RUNTIME_BUILD_FAILED
    return fallback


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    source_commit: str
    runtime_digest: str
    package_version: str
    package_versions: dict[str, str]
    packages: dict[str, PackageMetadata]
    authority: DependencyAuthority
    playwright_version: str
    python_minor: str
    platform_name: str
    architecture: str
    source_trees: dict[str, str]
    runtime_authority_digest: str
    dependency_metadata_digest: str


def _run(
    arguments: Sequence[str],
    *,
    cwd: Path,
    environment: dict[str, str] | None = None,
) -> str:
    try:
        result = bounded_command(
            list(arguments),
            cwd=cwd,
            env=os.environ if environment is None else environment,
            timeout=300,
        )
    except subprocess.TimeoutExpired as error:
        raise BootstrapStageError(
            _BUILD_STAGE.get(), "COMMAND_TIMED_OUT", type(error).__name__
        ) from error
    except OSError as error:
        raise BootstrapStageError(
            _BUILD_STAGE.get(), "COMMAND_UNAVAILABLE", type(error).__name__
        ) from error
    if result.returncode != 0:
        raise BootstrapStageError(
            _BUILD_STAGE.get(),
            "COMMAND_FAILED",
            (result.stdout + b"\0" + result.stderr).decode("utf-8", errors="replace"),
        )
    return result.stdout.decode("utf-8").rstrip("\n")


def _git(repo_root: Path, *arguments: str) -> str:
    try:
        return git_output(repo_root, *arguments)
    except GitExecutionError as error:
        raise BootstrapError("required Git command failed") from error


def safe_git_environment() -> dict[str, str]:
    return _git_execution_environment()


def _validate_origin(url: str) -> None:
    try:
        canonical_repository_identity(url)
    except RepositoryIdentityError as error:
        raise RepositoryIdentityInvalid(
            "origin does not identify the expected repository"
        ) from error


def validate_repository(repo_root: Path, source_ref: str) -> tuple[Path, str]:
    try:
        identity = validate_repository_identity(repo_root)
    except RepositoryIdentityError as error:
        raise RepositoryIdentityInvalid(
            "repository execution identity was rejected"
        ) from error
    try:
        commit = git_output(
            identity.root,
            "rev-parse",
            "--verify",
            f"{source_ref}^{{commit}}",
        )
    except GitExecutionError as error:
        raise RepositoryIdentityInvalid(
            "source ref did not resolve to an exact commit"
        ) from error
    if SHA_RE.fullmatch(commit) is None:
        raise RepositoryIdentityInvalid("source ref did not resolve to an exact commit")
    return identity.root, commit


def fetch_origin_main(repo_root: Path) -> None:
    try:
        repository = validate_repository_identity(repo_root).root
    except RepositoryIdentityError as error:
        raise RepositoryIdentityInvalid(
            "origin/main fetch authority was rejected"
        ) from error
    try:
        git_output(repository, "fetch", "--no-tags", "origin", "main")
    except GitExecutionError as error:
        raise RemoteFetchFailed("origin/main could not be fetched") from error


def _git_object_exists(repo_root: Path, spec: str) -> bool:
    try:
        return git_succeeds(repo_root, "cat-file", "-e", spec)
    except GitExecutionError as error:
        raise BootstrapError("Git metadata could not be read") from error


def _git_blob(repo_root: Path, spec: str) -> bytes:
    try:
        return git_bytes(repo_root, "show", spec)
    except GitExecutionError as error:
        raise BootstrapError("Git metadata could not be read") from error


def _compute_runtime_identity(
    repo_root: Path,
    source_commit: str,
    *,
    host_python_minor: str,
) -> RuntimeIdentity:
    try:
        authority = parse_authority(
            _git_blob(repo_root, f"{source_commit}:{AUTHORITY_FILENAME}")
        )
        policies_by_path = {
            policy.path: policy for policy in authority.packages.values()
        }
        if set(policies_by_path) != set(SKILL_PATHS):
            raise AuthorityError(
                "Runtime package paths did not match the fixed registry"
            )
        if {
            path: policy.module for path, policy in policies_by_path.items()
        } != PACKAGE_MODULES:
            raise AuthorityError(
                "Runtime package modules did not match the fixed registry"
            )
        pyprojects: dict[str, bytes] = {}
        module_versions: dict[str, bytes] = {}
        source_trees: dict[str, str] = {}
        for package_name, policy in authority.packages.items():
            source_spec = f"{source_commit}:{policy.path}/src"
            if not _git_object_exists(repo_root, source_spec):
                raise AuthorityError(f"{package_name} source tree was missing")
            tree_sha = _git(repo_root, "rev-parse", source_spec)
            if SHA_RE.fullmatch(tree_sha) is None:
                raise AuthorityError(f"{package_name} source tree identity was invalid")
            source_trees[policy.path] = tree_sha
            pyprojects[package_name] = _git_blob(
                repo_root,
                f"{source_commit}:{policy.path}/pyproject.toml",
            )
            module_versions[package_name] = _git_blob(
                repo_root,
                f"{source_commit}:{policy.path}/src/{policy.module}/__init__.py",
            )
        packages = validate_package_metadata(
            authority,
            pyprojects,
            module_versions,
        )
    except AuthorityError as error:
        raise BootstrapError("dependency authority validation failed") from error
    if host_python_minor != authority.python_minor:
        raise BootstrapError("host Python did not match the Runtime authority")
    platform_name = platform.system().lower()
    architecture = platform.machine().lower()
    runtime_authority_digest = hashlib.sha256(
        canonical_bytes(authority.runtime_document())
    ).hexdigest()
    dependency_digest = hashlib.sha256(
        canonical_bytes(
            {
                "packages": {
                    name: package.production_document
                    for name, package in sorted(packages.items())
                }
            }
        )
    ).hexdigest()
    digest_input = {
        "architecture": architecture,
        "dependency_metadata_digest": dependency_digest,
        "platform": platform_name,
        "python_minor": host_python_minor,
        "runtime_authority_digest": runtime_authority_digest,
        "runtime_identity_schema": 2,
        "source_trees": source_trees,
    }
    runtime_digest = hashlib.sha256(
        json.dumps(digest_input, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    package_versions = {package.path: package.version for package in packages.values()}
    shared_package = packages["hoyo-browser-session"]
    return RuntimeIdentity(
        source_commit=source_commit,
        runtime_digest=runtime_digest,
        package_version=shared_package.version,
        package_versions=package_versions,
        packages=packages,
        authority=authority,
        playwright_version=authority.runtime_dependencies["playwright"],
        python_minor=host_python_minor,
        platform_name=platform_name,
        architecture=architecture,
        source_trees=source_trees,
        runtime_authority_digest=runtime_authority_digest,
        dependency_metadata_digest=dependency_digest,
    )


def compute_runtime_identity(repo_root: Path, source_commit: str) -> RuntimeIdentity:
    try:
        return _compute_runtime_identity(
            repo_root,
            source_commit,
            host_python_minor=f"{sys.version_info.major}.{sys.version_info.minor}",
        )
    except BootstrapError as error:
        raise RuntimeMetadataInvalid(
            "Runtime dependency metadata is invalid"
        ) from error


def _safe_parent_directory(path: Path) -> Path:
    absolute = path.expanduser().absolute()
    try:
        if not os.path.lexists(absolute):
            absolute.mkdir(parents=True, mode=0o700, exist_ok=True)
        metadata = absolute.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise BootstrapError("external parent directory is unsafe")
    except OSError as error:
        raise BootstrapError(
            "external parent directory could not be secured"
        ) from error
    return absolute.resolve(strict=True)


def _private_directory(path: Path) -> Path:
    absolute = path.expanduser().absolute()
    try:
        if os.path.lexists(absolute):
            metadata = absolute.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise BootstrapError("external directory is unsafe")
        else:
            absolute.mkdir(parents=True, mode=0o700, exist_ok=True)
            metadata = absolute.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise BootstrapError("external directory is unsafe")
    except OSError as error:
        raise BootstrapError("external directory could not be secured") from error
    return absolute.resolve(strict=True)


def _private_file(path: Path) -> Path:
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise BootstrapError("external file is unsafe")
        finally:
            os.close(descriptor)
    except OSError as error:
        raise BootstrapError("external file could not be secured") from error
    return path.resolve(strict=True)


def _lock_is_held(path: Path) -> bool:
    if not os.path.lexists(path):
        return False
    safe = _private_file(path)
    descriptor = os.open(safe, os.O_RDWR)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def _assert_owner_inactive(external_root: Path) -> None:
    if _lock_is_held(external_root / "broker/worker.lock") or _lock_is_held(
        external_root / "browser/profile.lock"
    ):
        raise BootstrapStageError(
            "OWNER_PREFLIGHT", "OWNER_SHUTDOWN_REQUIRED", "active Owner lock"
        )
    database = external_root / "broker/state.sqlite3"
    if not os.path.lexists(database):
        return
    safe = _private_file(database)
    try:
        with contextlib.closing(
            sqlite3.connect(safe.as_uri() + "?mode=ro", uri=True)
        ) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(worker_state)")
            }
            if "owner_phase" not in columns:
                return  # Older schemas are migrated by _bootstrap-init.
            row = connection.execute(
                "SELECT owner_phase, worker_id, pid FROM worker_state WHERE singleton_id = 1"
            ).fetchone()
            if row is None or row != ("STOPPED", None, None):
                raise BootstrapStageError(
                    "OWNER_PREFLIGHT",
                    "OWNER_RECOVERY_REQUIRED",
                    "inactive Owner record is not clean",
                )
    except sqlite3.Error as error:
        raise BootstrapStageError(
            "OWNER_PREFLIGHT", "OWNER_STATE_UNAVAILABLE", type(error).__name__
        ) from error


@contextlib.contextmanager
def _bootstrap_lock(runtime_root: Path) -> Iterator[None]:
    path = _private_file(runtime_root / "bootstrap.lock")
    descriptor = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _runtime_environment(external_root: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["PLAYWRIGHT_BROWSERS_PATH"] = str(
        external_root / PLAYWRIGHT_BROWSERS_RELATIVE
    )
    return environment


def _chromium_executable(
    venv_python: Path, *, cwd: Path, environment: dict[str, str]
) -> Path:
    output = _run(
        (
            str(venv_python),
            "-c",
            "from playwright.sync_api import sync_playwright;"
            "p=sync_playwright().start();"
            "print(p.chromium.executable_path);"
            "p.stop()",
        ),
        cwd=cwd,
        environment=environment,
    )
    return Path(output)


def _ensure_chromium(
    venv_python: Path,
    *,
    cwd: Path,
    environment: dict[str, str],
) -> bool:
    if _chromium_executable(venv_python, cwd=cwd, environment=environment).is_file():
        return False
    _run(
        (str(venv_python), "-m", "playwright", "install", "chromium"),
        cwd=cwd,
        environment=environment,
    )
    return True


def _required_metadata_header(message: Message, name: str) -> str:
    value = message.get(name)
    if type(value) is not str or not value:
        raise BootstrapError("wheel metadata required header was invalid")
    return value


def _distribution_document_from_message(message: Message) -> dict[str, object]:
    return {
        "name": _required_metadata_header(message, "Name"),
        "provides_extra": list(message.get_all("Provides-Extra", [])),
        "requires_dist": list(message.get_all("Requires-Dist", [])),
        "requires_python": _required_metadata_header(message, "Requires-Python"),
        "version": _required_metadata_header(message, "Version"),
    }


def _wheel_distribution_document(path: Path) -> dict[str, object]:
    try:
        with zipfile.ZipFile(path) as archive:
            metadata_names = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise BootstrapError("built wheel metadata path was invalid")
            message = BytesParser().parsebytes(archive.read(metadata_names[0]))
    except (OSError, zipfile.BadZipFile, KeyError) as error:
        raise BootstrapError("built wheel metadata could not be read") from error
    return _distribution_document_from_message(message)


def _validate_wheelhouse(
    wheelhouse: Path,
    packages: Mapping[str, PackageMetadata],
) -> tuple[Path, ...]:
    wheels = tuple(sorted(wheelhouse.glob("*.whl")))
    if len(wheels) != len(packages):
        raise BootstrapError("built wheel set did not match source packages")
    validated: dict[str, Path] = {}
    try:
        for wheel in wheels:
            document = _wheel_distribution_document(wheel)
            name = document["name"]
            if type(name) is not str or name not in packages or name in validated:
                raise BootstrapError("built wheel package identity was invalid")
            validate_distribution_document(packages[name], document)
            validated[name] = wheel
    except AuthorityError as error:
        raise BootstrapError("built wheel metadata did not match source") from error
    if set(validated) != set(packages):
        raise BootstrapError("built wheel package set was incomplete")
    return tuple(validated[name] for name in sorted(validated))


def _validate_installed_metadata(
    python: Path,
    *,
    repo_root: Path,
    environment: dict[str, str],
    authority: DependencyAuthority,
    packages: Mapping[str, PackageMetadata],
    include_dev: bool,
) -> None:
    specification = {
        "dependencies": sorted(expected_installed_versions(authority, packages)),
        "packages": {
            name: package.module for name, package in sorted(packages.items())
        },
    }
    script = (
        "import importlib,json,sys;"
        "from importlib import metadata;"
        "spec=json.loads(sys.argv[1]);"
        "result={'dependencies':{},'packages':{}};"
        "[(result['dependencies'].__setitem__(name,metadata.version(name)))"
        " for name in spec['dependencies']];"
        "[(lambda module,dist:"
        " result['packages'].__setitem__(name,{"
        "'module_version':getattr(module,'__version__',None),"
        "'name':dist.get('Name'),"
        "'provides_extra':dist.get_all('Provides-Extra',[]),"
        "'requires_dist':dist.get_all('Requires-Dist',[]),"
        "'requires_python':dist.get('Requires-Python'),"
        "'version':dist.get('Version')}))"
        "(importlib.import_module(module_name),metadata.metadata(name))"
        " for name,module_name in spec['packages'].items()];"
        "print(json.dumps(result,sort_keys=True,separators=(',',':')))"
    )
    output = _run(
        (str(python), "-c", script, json.dumps(specification, sort_keys=True)),
        cwd=repo_root,
        environment=environment,
    )
    try:
        payload: object = json.loads(output)
        if not isinstance(payload, dict) or set(payload) != {
            "dependencies",
            "packages",
        }:
            raise BootstrapError("installed package inspection result was invalid")
        installed_dependencies = payload["dependencies"]
        installed_packages = payload["packages"]
        if not isinstance(installed_dependencies, dict) or not isinstance(
            installed_packages,
            dict,
        ):
            raise BootstrapError("installed package inspection fields were invalid")
        if installed_dependencies != expected_installed_versions(authority, packages):
            raise BootstrapError(
                "installed dependency versions did not match the authority"
            )
        if set(installed_packages) != set(packages):
            raise BootstrapError("installed package set did not match source")
        for name, contract in packages.items():
            raw_document = installed_packages[name]
            if not isinstance(raw_document, dict):
                raise BootstrapError("installed package metadata was invalid")
            module_version = raw_document.pop("module_version", None)
            if module_version != contract.version:
                raise BootstrapError("installed module version did not match source")
            validate_distribution_document(
                contract,
                raw_document,
                include_dev=include_dev,
            )
    except (json.JSONDecodeError, AuthorityError) as error:
        raise BootstrapError(
            "installed package metadata did not match source"
        ) from error


def _generation_package_ready(
    generation: Path,
    *,
    repo_root: Path,
    environment: dict[str, str],
    authority: DependencyAuthority,
    expected_packages: Mapping[str, PackageMetadata],
) -> bool:
    python = generation / "venv/bin/python"
    if not python.is_file() or not os.access(python, os.X_OK):
        return False
    try:
        _validate_installed_metadata(
            python,
            repo_root=repo_root,
            environment=environment,
            authority=authority,
            packages=expected_packages,
            include_dev=False,
        )
        return True
    except BootstrapError:
        return False


def _generation_ready(
    generation: Path,
    *,
    repo_root: Path,
    environment: dict[str, str],
    authority: DependencyAuthority,
    expected_packages: Mapping[str, PackageMetadata],
) -> bool:
    if not _generation_package_ready(
        generation,
        repo_root=repo_root,
        environment=environment,
        authority=authority,
        expected_packages=expected_packages,
    ):
        return False
    return _chromium_executable(
        generation / "venv/bin/python",
        cwd=repo_root,
        environment=environment,
    ).is_file()


def _environment_gaps(
    target: Path,
    active: dict[str, Any] | None,
    identity: RuntimeIdentity,
    *,
    repo_root: Path,
    environment: dict[str, str],
) -> tuple[str, ...]:
    gaps: list[str] = []
    if not target.exists():
        gaps.append("RUNTIME_GENERATION")
    elif not _generation_ready(
        target,
        repo_root=repo_root,
        environment=environment,
        authority=identity.authority,
        expected_packages=identity.packages,
    ):
        gaps.append("RUNTIME_GENERATION_INCOMPLETE")
    if active is None or active.get("runtime_digest") != identity.runtime_digest:
        gaps.append("ACTIVE_MARKER")
    return tuple(gaps)


def _accept_existing_generation(
    generation: Path,
    *,
    repo_root: Path,
    external_root: Path,
    environment: dict[str, str],
    authority: DependencyAuthority,
    expected_packages: Mapping[str, PackageMetadata],
) -> bool:
    if not _generation_package_ready(
        generation,
        repo_root=repo_root,
        environment=environment,
        authority=authority,
        expected_packages=expected_packages,
    ):
        raise BootstrapError("immutable Runtime generation is incomplete")
    python = generation / "venv/bin/python"
    browser_installed = _ensure_chromium(
        python,
        cwd=repo_root,
        environment=environment,
    )
    _run(
        (
            str(python),
            "-m",
            "hoyo_browser_session",
            "_bootstrap-init",
            "--repo-root",
            str(repo_root),
            "--external-root",
            str(external_root),
        ),
        cwd=repo_root,
        environment=environment,
    )
    _run(
        (
            str(python),
            "-m",
            "hoyo_browser_session",
            "browser-launch-check",
            "--repo-root",
            str(repo_root),
            "--external-root",
            str(external_root),
        ),
        cwd=repo_root,
        environment=environment,
    )
    return browser_installed


def _read_active(path: Path) -> dict[str, Any] | None:
    if not os.path.lexists(path):
        return None
    try:
        safe = _private_file(path)
    except BootstrapError as error:
        raise RuntimeMetadataInvalid("active Runtime marker is unsafe") from error
    try:
        payload: Any = json.loads(safe.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeMetadataInvalid("active Runtime marker is invalid") from error
    if not isinstance(payload, dict) or set(payload) != ACTIVE_KEYS:
        raise RuntimeMetadataInvalid("active Runtime marker fields are invalid")
    digest = payload.get("runtime_digest")
    if type(digest) is not str or DIGEST_RE.fullmatch(digest) is None:
        raise RuntimeMetadataInvalid("active Runtime marker digest is invalid")
    return payload


def _write_active(path: Path, identity: RuntimeIdentity) -> None:
    payload = {
        "architecture": identity.architecture,
        "created_at": time.time(),
        "platform": identity.platform_name,
        "playwright_version": identity.playwright_version,
        "python_minor": identity.python_minor,
        "runtime_digest": identity.runtime_digest,
        "schema_version": 1,
        "source_commit": identity.source_commit,
    }
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException as error:
        with contextlib.suppress(OSError):
            if os.path.lexists(temporary):
                os.unlink(temporary)
        if isinstance(error, KeyboardInterrupt | SystemExit):
            raise
        raise RuntimeActivationFailed(
            "active Runtime marker could not be replaced"
        ) from error


def _create_source_worktree(
    repo_root: Path,
    source_commit: str,
    temporary_root: Path,
) -> Path:
    worktree = temporary_root / "worktree"
    _git(repo_root, "worktree", "add", "--detach", str(worktree), source_commit)
    worktree.chmod(0o700)
    if _git(worktree, "rev-parse", "HEAD") != source_commit:
        raise BootstrapError("temporary source worktree did not match the source ref")
    if _git(worktree, "status", "--porcelain"):
        raise BootstrapError("temporary source worktree was not clean")
    return worktree


def _remove_source_worktree(
    repo_root: Path, worktree: Path, temporary_root: Path
) -> None:
    _git(repo_root, "worktree", "remove", str(worktree))
    try:
        temporary_root.rmdir()
    except OSError as error:
        raise BootstrapError("temporary source worktree cleanup failed") from error


def _install_generation(
    repo_root: Path,
    source_commit: str,
    identity: RuntimeIdentity,
    *,
    external_root: Path,
    staging: Path,
) -> None:
    temporary_root = _private_directory(staging / ".source")
    worktree: Path | None = None
    environment = _runtime_environment(external_root)
    try:
        with _build_stage("SOURCE_CHECKOUT"):
            worktree = _create_source_worktree(repo_root, source_commit, temporary_root)
        with _build_stage("VENV_CREATE"):
            _run(
                (sys.executable, "-m", "venv", str(staging / "venv")),
                cwd=worktree,
                environment=environment,
            )
        venv_python = staging / "venv/bin/python"
        wheelhouse = staging / ".wheels"
        wheelhouse.mkdir(mode=0o700)
        package_sources = [str(worktree / path) for path in SKILL_PATHS]
        with _build_stage("WHEEL_BUILD"):
            _run(
                (
                    str(venv_python),
                    "-m",
                    "pip",
                    "wheel",
                    "--disable-pip-version-check",
                    "--no-deps",
                    "--wheel-dir",
                    str(wheelhouse),
                    *package_sources,
                ),
                cwd=worktree,
                environment=environment,
            )
        wheels = _validate_wheelhouse(wheelhouse, identity.packages)
        with _build_stage("PACKAGE_INSTALL"):
            _run(
                (
                    str(venv_python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    *(str(wheel) for wheel in wheels),
                ),
                cwd=worktree,
                environment=environment,
            )
        with _build_stage("DEPENDENCY_ACCEPTANCE"):
            _validate_installed_metadata(
                venv_python,
                repo_root=worktree,
                environment=environment,
                authority=identity.authority,
                packages=identity.packages,
                include_dev=True,
            )
        shutil.rmtree(wheelhouse)
        with _build_stage("CHROMIUM_INSTALL"):
            _ensure_chromium(venv_python, cwd=worktree, environment=environment)
        with _build_stage("STATE_INIT"):
            _run(
                (
                    str(venv_python),
                    "-m",
                    "hoyo_browser_session",
                    "_bootstrap-init",
                    "--repo-root",
                    str(worktree),
                    "--external-root",
                    str(external_root),
                ),
                cwd=worktree,
                environment=environment,
            )
        with _build_stage("CLI_ACCEPTANCE"):
            _run(
                (str(venv_python), "-m", "hoyo_browser_session", "--help"),
                cwd=worktree,
                environment=environment,
            )
        with _build_stage("BROWSER_ACCEPTANCE"):
            _run(
                (
                    str(venv_python),
                    "-m",
                    "hoyo_browser_session",
                    "browser-launch-check",
                    "--repo-root",
                    str(worktree),
                    "--external-root",
                    str(external_root),
                ),
                cwd=worktree,
                environment=environment,
            )
        if _git(worktree, "status", "--porcelain"):
            raise BootstrapError("Bootstrap modified the source worktree")
    finally:
        if worktree is not None and worktree.exists():
            _remove_source_worktree(repo_root, worktree, temporary_root)
        elif temporary_root.exists():
            try:
                temporary_root.rmdir()
            except OSError as error:
                raise BootstrapError("temporary Bootstrap cleanup failed") from error


def bootstrap(
    repo_root: Path, source_ref: str, external_root: Path
) -> tuple[str, RuntimeIdentity]:
    repository, source_commit = validate_repository(repo_root, source_ref)
    identity = compute_runtime_identity(repository, source_commit)
    root = _safe_parent_directory(external_root)
    if root == repository or root.is_relative_to(repository):
        raise BootstrapError("external Runtime root must be outside the repository")
    runtimes_root = _safe_parent_directory(root / RUNTIME_RELATIVE.parts[0])
    runtime_root = _private_directory(runtimes_root / RUNTIME_RELATIVE.parts[1])
    generations = _private_directory(runtime_root / "generations")
    browser_root = _private_directory(root / PLAYWRIGHT_BROWSERS_RELATIVE.parts[0])
    _private_directory(browser_root / PLAYWRIGHT_BROWSERS_RELATIVE.parts[1])
    active_path = runtime_root / "active.json"
    environment = _runtime_environment(root)
    target = generations / identity.runtime_digest
    with _bootstrap_lock(runtime_root):
        active = _read_active(active_path)
        gaps = _environment_gaps(
            target,
            active,
            identity,
            repo_root=repository,
            environment=environment,
        )
        if not gaps:
            return "REUSED", identity
        _assert_owner_inactive(root)
        if target.exists():
            browser_installed = _accept_existing_generation(
                target,
                repo_root=repository,
                external_root=root,
                environment=environment,
                authority=identity.authority,
                expected_packages=identity.packages,
            )
            if not _generation_ready(
                target,
                repo_root=repository,
                environment=environment,
                authority=identity.authority,
                expected_packages=identity.packages,
            ):
                raise BootstrapError("immutable Runtime generation is incomplete")
            _write_active(active_path, identity)
            disposition = (
                "REPAIRED_BROWSER" if browser_installed else "ACTIVATED_EXISTING"
            )
            return disposition, identity
        staging = generations / f".staging-{uuid.uuid4().hex}"
        staging.mkdir(mode=0o700)
        try:
            _install_generation(
                repository,
                source_commit,
                identity,
                external_root=root,
                staging=staging,
            )
            if not _generation_ready(
                staging,
                repo_root=repository,
                environment=environment,
                authority=identity.authority,
                expected_packages=identity.packages,
            ):
                raise BootstrapError("staging Runtime acceptance failed")
            os.replace(staging, target)
            _write_active(active_path, identity)
        except BaseException:
            if staging.exists():
                shutil.rmtree(staging)
            raise
        return "INSTALLED", identity


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bootstrap_hoyo_runtime.py")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--source-ref", default="origin/main")
    parser.add_argument("--external-root", type=Path, default=DEFAULT_EXTERNAL_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        disposition, identity = bootstrap(
            args.repo_root,
            args.source_ref,
            args.external_root,
        )
    except KeyboardInterrupt:
        print("BOOTSTRAP=FAIL", file=sys.stderr)
        print(f"ERROR_CODE={LauncherErrorCode.INTERRUPTED.value}", file=sys.stderr)
        return 130
    except Exception as error:
        print("BOOTSTRAP=FAIL", file=sys.stderr)
        print(
            "ERROR_CODE="
            f"{classify_launcher_error(error, fallback=LauncherErrorCode.RUNTIME_BUILD_FAILED).value}",
            file=sys.stderr,
        )
        print_launcher_diagnostics(error)
        return 1
    print("BOOTSTRAP=PASS")
    print(f"BOOTSTRAP_DISPOSITION={disposition}")
    print(f"RUNTIME_DIGEST={identity.runtime_digest}")
    print(f"SOURCE_COMMIT={identity.source_commit}")
    print(f"HOYO_BROWSER_SESSION_VERSION={identity.package_versions[SKILL_PATHS[0]]}")
    print(f"GENSHIN_SKILL_VERSION={identity.package_versions[SKILL_PATHS[1]]}")
    print(f"ZZZ_SKILL_VERSION={identity.package_versions[SKILL_PATHS[2]]}")
    print(f"HSR_SKILL_VERSION={identity.package_versions[SKILL_PATHS[3]]}")
    print("FORMAL_DATA_CHANGED=NO")
    print("LIVE_HOYO_REQUEST_COUNT=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
