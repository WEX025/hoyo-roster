"""Deterministic preparation: authorized fast-forward, matching install, pinned JS."""

from hashlib import sha256
import json
from pathlib import Path
import sys

from .errors import WorkspaceError
from .user_workspace import private_directory
from .installation import command as command, repository_git, installation_environment


def inventory(directory):
    return {
        p.relative_to(directory).as_posix(): sha256(p.read_bytes()).hexdigest()
        for p in directory.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    }


def prepare(store):
    value = store.read()
    config = value["config"]
    package = Path(config["package_directory"])
    repository = Path(config["repository"]) if config["repository"] else None
    source_sha = None
    if repository:

        def git(*args, timeout=5):
            return command(repository_git(repository, *args), cwd=repository, timeout=timeout)

        if git("status", "--porcelain").strip():
            raise WorkspaceError("WORKFLOW_SOURCE_DIRTY")
        if config["update"] and value["version"] is None:
            upstream = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}").strip()
            git("fetch", "origin", timeout=30)
            git("merge", "--ff-only", upstream, timeout=15)
        source_sha = git("rev-parse", "HEAD").strip()
    expected = inventory(package)
    if not expected:
        raise WorkspaceError("WORKFLOW_PACKAGE_MISSING")
    digest = sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()
    if value["version"] and value["version"]["package_digest"] != digest:
        raise WorkspaceError("WORKFLOW_VERSION_CHANGED")
    # Isolated import prevents the control bootstrap's source path from concealing
    # a stale installed worker. No browser or authentication material is involved.
    probe = """import json, pathlib, hashlib, hoyo_roster
p=pathlib.Path(hoyo_roster.__file__).parent
print(json.dumps({f.relative_to(p).as_posix(): hashlib.sha256(f.read_bytes()).hexdigest()
 for f in p.rglob('*') if f.is_file() and '__pycache__' not in f.parts and f.suffix!='.pyc'}))"""
    try:
        installed = json.loads(command([sys.executable, "-I", "-c", probe], timeout=10))
    except WorkspaceError:
        installed = None
    repaired = False
    if installed != expected:
        if not config["repair_installation"]:
            raise WorkspaceError("WORKFLOW_INSTALLATION_MISMATCH")
        target = config.get("installation_artifact") or (str(repository) if repository else None)
        if target is None:
            raise WorkspaceError("WORKFLOW_INSTALLATION_SOURCE_REQUIRED")
        # Desktop sandbox identities can inherit an inaccessible Windows TEMP.
        # Keep pip's transient build state in this run's verified private scope.
        environment = installation_environment(store.root / "preparation-temp")
        command(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-cache-dir",
                "--no-deps",
                "--no-build-isolation",
                "--force-reinstall",
                target,
            ],
            timeout=120,
            env=environment,
        )
        installed = json.loads(command([sys.executable, "-I", "-c", probe], timeout=10))
        repaired = True
    if installed != expected:
        raise WorkspaceError("WORKFLOW_INSTALLATION_MISMATCH")
    # Version-addressed copies avoid stale ESM dependency caches after update or
    # install in a long-lived client. Only public JavaScript is copied, no browser state.
    scripts = private_directory(store.root / "code" / digest)
    for name in expected:
        if name.endswith(".mjs"):
            source, target = package / name, scripts / name
            private_directory(target.parent)
            if target.exists() and target.read_bytes() != source.read_bytes():
                raise WorkspaceError("WORKFLOW_CODE_CHANGED")
            if not target.exists():
                target.write_bytes(source.read_bytes())
                from . import platform_io

                platform_io.set_private(target, 0o600)
    return {
        "source_sha": source_sha,
        "package_digest": digest,
        "checked_package_files": len(expected),
        "installation_matches_source": True,
        "installation_repaired": repaired,
        "scripts": str(scripts),
    }
