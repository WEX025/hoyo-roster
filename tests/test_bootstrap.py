"""Cold bootstrap shares real private-directory and installation boundaries."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from hoyo_roster import bootstrap, installation, platform_io as io
from hoyo_roster.errors import WorkspaceError


def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname="hoyo-roster"\n', encoding="utf-8")
    return root


@pytest.mark.parametrize("failed", [False, True])
def test_cold_install_uses_same_private_environment_and_anchored_git(tmp_path, monkeypatch, failed):
    root = repository(tmp_path)
    calls = []
    monkeypatch.setenv("TEMP", str(tmp_path / "inaccessible-never-created"))
    monkeypatch.setenv("TMP", str(tmp_path / "inaccessible-never-created"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "inaccessible-never-created"))

    def execute(args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == "git":
            assert args[1:3] == ["-c", f"safe.directory={root.resolve().as_posix()}"]
            return ""
        environment = kwargs["env"]
        temp = Path(environment["TEMP"])
        assert environment["TMP"] == environment["TMPDIR"] == str(temp)
        assert io.private(temp) and io.owned(temp)
        assert temp.parent == io.filesystem_path(tmp_path)
        assert not (tmp_path / "run").exists()
        assert environment["PIP_NO_INPUT"] == environment["PYTHONUTF8"] == "1"
        if failed:
            raise WorkspaceError("WORKFLOW_PREPARATION_FAILED")
        return ""

    monkeypatch.setattr(bootstrap, "command", execute)
    if failed:
        with pytest.raises(WorkspaceError, match="WORKFLOW_ENVIRONMENT_INSTALL_FAILED"):
            bootstrap.ensure_dependencies(tmp_path / "run", root, None)
    else:
        bootstrap.ensure_dependencies(tmp_path / "run", root, None)
    assert len(calls) == 2
    assert not list(tmp_path.glob(".hoyo-roster-bootstrap-*"))
    assert not (tmp_path / "inaccessible-never-created").exists()


def test_dirty_source_stops_before_pip_or_temporary_creation(tmp_path, monkeypatch):
    root = repository(tmp_path)
    monkeypatch.setattr(bootstrap, "command", lambda *a, **k: " M synthetic.py\n")
    with pytest.raises(WorkspaceError, match="WORKFLOW_SOURCE_DIRTY"):
        bootstrap.ensure_dependencies(tmp_path / "run", root, None)
    assert not list(tmp_path.glob(".hoyo-roster-bootstrap-*"))


def test_bootstrap_rejects_source_overlap_before_install(tmp_path, monkeypatch):
    root = repository(tmp_path)
    monkeypatch.setattr(
        bootstrap, "command", lambda *a, **k: pytest.fail("unsafe scope reached Git/pip")
    )
    with pytest.raises(WorkspaceError, match="UNSAFE_WORKFLOW_DIRECTORY"):
        bootstrap.ensure_dependencies(root / "run", root, None)
    assert sorted(p.name for p in root.iterdir()) == ["pyproject.toml"]


def test_installation_subprocess_cannot_inherit_browser_pipe(monkeypatch):
    seen = []

    def execute(args, **kwargs):
        seen.append(kwargs)
        return subprocess.CompletedProcess(args, 0, b"ready", b"")

    monkeypatch.setattr(installation.subprocess, "run", execute)
    assert installation.command(["synthetic"]) == "ready"
    assert seen[0]["stdin"] is subprocess.DEVNULL


def test_bootstrap_cleanup_failure_preserves_private_scope(tmp_path, monkeypatch):
    from hoyo_roster.storage import common

    root = repository(tmp_path)
    monkeypatch.setattr(bootstrap, "command", lambda *a, **k: "")

    def refuse(path):
        raise common.MetadataError("synthetic unknown contents")

    monkeypatch.setattr(common, "remove_owned_tree", refuse)
    with pytest.raises(WorkspaceError, match="WORKFLOW_BOOTSTRAP_CLEANUP_PENDING"):
        bootstrap.ensure_dependencies(tmp_path / "run", root, None)
    retained = list(tmp_path.glob(".hoyo-roster-bootstrap-*"))
    assert len(retained) == 1 and io.private(retained[0])


def test_platform_dependencies_missing_returns_fixed_error_before_install(tmp_path):
    # Isolated subprocess blocks native permission imports. It must emit only
    # a fixed status and create no work directory, on either host OS.
    script = """import builtins,runpy,sys
sys.path.insert(0,sys.argv.pop(1))
original=builtins.__import__
def importing(name,*args,**kwargs):
    if name == 'platform_io' or name.endswith('.platform_io') or (len(args)>2 and 'platform_io' in (args[2] or ())):
        raise ImportError('synthetic native provider missing')
    return original(name,*args,**kwargs)
builtins.__import__=importing
runpy.run_module('hoyo_roster.bootstrap',run_name='__main__')
"""
    request = {"directory": str(tmp_path / "run"), "repository": None, "artifact": "synthetic.whl"}
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(Path(__file__).resolve().parents[1] / "src")],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout) == {
        "ok": False,
        "errorCode": "WORKFLOW_PLATFORM_DEPENDENCIES_REQUIRED",
    }
    assert not list(tmp_path.iterdir())
