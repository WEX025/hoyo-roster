"""Find this project's source checkout, never choose a repository from the cwd."""

from __future__ import annotations

import json
import subprocess
import tomllib
from importlib import metadata
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname
from platformdirs import user_data_path
from .installation import repository_git as repository_git


def default_private_root() -> Path:
    return user_data_path("hoyo-roster", appauthor=False, roaming=False)


def verified_repository(candidate: Path) -> Path | None:
    candidate = candidate.resolve()
    try:
        project = tomllib.loads((candidate / "pyproject.toml").read_text(encoding="utf-8"))
        if project["project"]["name"] != "hoyo-roster":
            return None
        if not (candidate / "src/hoyo_roster/cli.py").is_file():
            return None
        if not (candidate / ".git").exists():
            return None
        result = subprocess.run(
            repository_git(candidate, "-C", str(candidate), "rev-parse", "--show-toplevel"),
            capture_output=True,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
        if result.returncode == 0 and Path(result.stdout.decode().strip()).resolve() == candidate:
            return candidate
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        pass
    return None


def project_repository() -> Path | None:
    # A source or editable import is anchored to this module, not the working directory.
    module = Path(__file__).resolve()
    if module.parent.name == "hoyo_roster" and module.parent.parent.name == "src":
        root = verified_repository(module.parents[2])
        if root is not None:
            return root
    # pip install . copies code to site-packages. PEP 610 retains the local source URL.
    # Only an existing verified source checkout can authorize repository-local output.
    try:
        value = metadata.distribution("hoyo-roster").read_text("direct_url.json")
        direct = json.loads(value) if value else {}
        url = urlsplit(direct.get("url", ""))
        if "dir_info" in direct and url.scheme == "file" and url.netloc in {"", "localhost"}:
            return verified_repository(Path(url2pathname(url.path)))
    except (metadata.PackageNotFoundError, OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def default_storage_root() -> Path:
    repository = project_repository()
    return repository / "data" if repository else default_private_root() / "results"


def containing_repository(path: Path) -> Path | None:
    for parent in (path, *path.parents):
        if (parent / ".git").exists():
            return parent
    return None
