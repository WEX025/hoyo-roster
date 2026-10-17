"""Minimal dependency bootstrap; delegates filesystem/process policy, never games."""

import importlib
import json
from pathlib import Path
import sys
import tomllib
import uuid

from .errors import WorkspaceError
from .installation import (
    command,
    repository_git,
    installation_environment,
    validate_workflow_directory,
)


def ensure_dependencies(directory: Path, repository: Path | None, artifact: str | None):
    # Native permission support is the only irreducible Windows prerequisite.
    # No pip command or directory creation may precede this capability check.
    try:
        from . import platform_io as io
        from .storage.common import MetadataError, remove_owned_tree
    except ImportError:
        raise WorkspaceError("WORKFLOW_PLATFORM_DEPENDENCIES_REQUIRED") from None
    io.check_components(directory)
    validate_workflow_directory(directory, repository, Path(__file__).resolve().parent)
    target = artifact or (str(repository) if repository else None)
    if target is None:
        raise WorkspaceError("WORKFLOW_INSTALLATION_SOURCE_REQUIRED")
    if repository is not None:
        try:
            if (
                tomllib.loads((repository / "pyproject.toml").read_text(encoding="utf-8"))[
                    "project"
                ]["name"]
                != "hoyo-roster"
            ):
                raise ValueError()
            if command(
                repository_git(repository, "status", "--porcelain"), cwd=repository, timeout=5
            ).strip():
                raise WorkspaceError("WORKFLOW_SOURCE_DIRTY")
        except (OSError, ValueError, KeyError) as error:
            if isinstance(error, WorkspaceError):
                raise
            raise WorkspaceError("WORKFLOW_SOURCE_UNVERIFIED") from None
    # Creating the run itself is reserved for RunStore.create. Only this fresh,
    # private sibling belongs to bootstrap, not a result/session/runtime root.
    temporary = io.filesystem_path(directory.parent / f".hoyo-roster-bootstrap-{uuid.uuid4()}")
    environment = installation_environment(temporary)
    try:
        command(
            [sys.executable, "-m", "pip", "install", "--no-cache-dir", target],
            env=environment,
            timeout=180,
        )
        importlib.invalidate_caches()
        for name in ("portalocker", "platformdirs", "jsonschema"):
            importlib.import_module(name)
    except (WorkspaceError, ImportError):
        raise WorkspaceError("WORKFLOW_ENVIRONMENT_INSTALL_FAILED") from None
    finally:
        # Refuse unknown/redirected contents rather than blindly deleting them.
        try:
            io.check_components(temporary)
            remove_owned_tree(temporary)
        except (MetadataError, io.UnsafePath, OSError):
            raise WorkspaceError("WORKFLOW_BOOTSTRAP_CLEANUP_PENDING") from None


def main():
    try:
        request = json.load(sys.stdin)
        directory = Path(request["directory"])
        repository = Path(request["repository"]) if request.get("repository") else None
        if not directory.is_absolute() or (repository is not None and not repository.is_absolute()):
            raise WorkspaceError("WORKFLOW_OPTIONS_INVALID")
        ensure_dependencies(directory, repository, request.get("artifact"))
        result = {"ok": True}
    except WorkspaceError as error:
        result = {"ok": False, "errorCode": str(error)}
    except Exception:
        result = {"ok": False, "errorCode": "WORKFLOW_BOOTSTRAP_FAILED"}
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
