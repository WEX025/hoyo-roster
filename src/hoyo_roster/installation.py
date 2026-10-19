"""Source/install inventories shared by workflow preparation and verification."""

import hashlib
import os
import subprocess
import sys
from pathlib import Path

from .errors import WorkspaceError


def repository_git(candidate: Path, *args: str) -> list[str]:
    """Trust only the selected checkout for one command, never global Git config."""
    return ["git", "-c", f"safe.directory={candidate.resolve().as_posix()}", *args]


def validate_workflow_directory(directory: Path, repository: Path | None, package: Path):
    """Bootstrap and RunStore reject the same overlapping source/result scopes."""
    root = directory.resolve()
    if repository is not None and (
        root.is_relative_to(repository.resolve()) or repository.resolve().is_relative_to(root)
    ):
        raise WorkspaceError("UNSAFE_WORKFLOW_DIRECTORY")
    if root.is_relative_to(package.resolve()) or package.resolve().is_relative_to(root):
        raise WorkspaceError("UNSAFE_WORKFLOW_DIRECTORY")


def command(args, *, cwd=None, timeout=120, env=None):
    """Shared preparation subprocess boundary, isolated from the browser pipe."""
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode:
            raise WorkspaceError("WORKFLOW_PREPARATION_FAILED")
        return result.stdout.decode("utf-8")
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        raise WorkspaceError("WORKFLOW_PREPARATION_FAILED") from None


def installation_environment(directory: Path) -> dict[str, str]:
    """Both bootstrap and repair use the same verified private temporary scope."""
    # These imports need no business/dependency packages. Windows needs its
    # native permission provider; never replace it with ignored chmod modes.
    from .storage.common import MetadataError, secure_private_directory
    from . import platform_io as io

    try:
        temporary = secure_private_directory(directory)
    except (MetadataError, io.UnsafePath, OSError):
        raise WorkspaceError("WORKFLOW_INSTALLATION_TEMP_UNSAFE") from None
    return dict(
        os.environ,
        TMPDIR=str(temporary),
        TEMP=str(temporary),
        TMP=str(temporary),
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
        PIP_NO_INPUT="1",
    )


def verify(root: Path, installed: Path) -> dict:
    source = root / "src/hoyo_roster"
    required = [
        path
        for path in source.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    ]
    expected_inventory = {path.relative_to(source).as_posix() for path in required}
    actual_inventory = {
        path.relative_to(installed).as_posix()
        for path in installed.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }
    unexpected = actual_inventory - expected_inventory
    mismatches = 0
    for path in required:
        target = installed / path.relative_to(source)
        if (
            not target.is_file()
            or hashlib.sha256(target.read_bytes()).digest()
            != hashlib.sha256(path.read_bytes()).digest()
        ):
            mismatches += 1
    revision = subprocess.run(
        repository_git(root, "rev-parse", "HEAD"),
        cwd=root,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
    )
    status = subprocess.run(
        repository_git(root, "status", "--porcelain"),
        cwd=root,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
    )
    clean = status.returncode == 0 and not status.stdout
    matched = bool(required) and mismatches == 0 and not unexpected
    return {
        "source_sha": revision.stdout.strip() if revision.returncode == 0 else None,
        "source_worktree_clean": clean,
        "loaded_package": str(installed),
        "python_executable": sys.executable,
        "checked_package_files": len(required),
        "mismatched_files": mismatches,
        "unexpected_installed_files": len(unexpected),
        "installation_matches_source": matched,
    }
