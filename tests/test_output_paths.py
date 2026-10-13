from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hoyo_roster import paths
from hoyo_roster.cli import build_parser, select_configuration
from hoyo_roster.storage.common import tree_digest
from hoyo_roster.user_workspace import LocalSnapshotStore, WorkspaceError, direct_configuration

UID = "123456789"
ROOT = Path(__file__).resolve().parents[1]


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True).stdout


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "clone"
    root.mkdir()
    git(root, "init", "-q")
    (root / "pyproject.toml").write_text('[project]\nname = "hoyo-roster"\n')
    (root / "src/hoyo_roster").mkdir(parents=True)
    (root / "src/hoyo_roster/cli.py").write_text("# synthetic project marker\n")
    (root / "data").mkdir(mode=0o755)
    (root / "data/.gitkeep").touch()
    (root / ".gitignore").write_text((ROOT / ".gitignore").read_text())
    return root


def installed_metadata(monkeypatch, root):
    monkeypatch.setattr(paths, "__file__", str(root.parent / "site-packages/hoyo_roster/paths.py"))
    monkeypatch.setattr(
        paths.metadata,
        "distribution",
        lambda name: SimpleNamespace(
            read_text=lambda file: json.dumps({"url": root.as_uri(), "dir_info": {}})
        ),
    )


@pytest.mark.parametrize("cwd_kind", ["root", "subdir", "other"])
@pytest.mark.parametrize("installation", ["source", "editable", "normal"])
def test_defaults_are_anchored_to_verified_project(
    checkout, tmp_path, monkeypatch, cwd_kind, installation
):
    if installation == "normal":
        installed_metadata(monkeypatch, checkout)
    else:
        monkeypatch.setattr(paths, "__file__", str(checkout / "src/hoyo_roster/paths.py"))
    cwd = (
        checkout
        if cwd_kind == "root"
        else (checkout / "docs" if cwd_kind == "subdir" else tmp_path / "other")
    )
    cwd.mkdir(exist_ok=True)
    if cwd_kind == "other":
        git(cwd, "init", "-q")
    monkeypatch.chdir(cwd)
    config, _, _ = select_configuration(
        build_parser().parse_args(["--game", "genshin", "--uid", UID])
    )
    assert config.storage_root == checkout / "data"
    assert config.auth_root.expanduser() == Path.home() / ".local/share/hoyo-roster"


@pytest.mark.parametrize("missing", ["wheel", "removed", "wrong-project", "not-git"])
def test_no_verified_source_falls_back_to_user_directory(checkout, monkeypatch, missing):
    installed_metadata(monkeypatch, checkout)
    if missing == "wheel":
        monkeypatch.setattr(
            paths.metadata,
            "distribution",
            lambda name: SimpleNamespace(read_text=lambda file: None),
        )
    elif missing == "removed":
        monkeypatch.setattr(
            paths.metadata,
            "distribution",
            lambda name: SimpleNamespace(
                read_text=lambda file: json.dumps(
                    {"url": (checkout / "missing").as_uri(), "dir_info": {}}
                )
            ),
        )
    elif missing == "wrong-project":
        (checkout / "pyproject.toml").write_text('[project]\nname = "other-project"\n')
    else:
        import shutil

        shutil.rmtree(checkout / ".git")
    monkeypatch.chdir(checkout)
    assert paths.project_repository() is None
    assert (
        paths.default_storage_root().expanduser()
        == Path.home() / ".local/share/hoyo-roster/results"
    )


def test_explicit_roots_and_auth_isolation(checkout, tmp_path, monkeypatch, source):
    monkeypatch.setattr(paths, "project_repository", lambda: checkout)
    stores = [
        LocalSnapshotStore(source, direct_configuration(game, uid), game)
        for game in ("genshin", "zzz")
        for uid in (UID, "987654321")
    ]
    assert len({s.root for s in stores}) == len({s.auth_root for s in stores}) == 4
    for store in stores:
        assert store.root.is_relative_to(checkout / "data")
        assert not store.auth_root.is_relative_to(checkout)
        assert store.browser_profile.parent == store.auth_root
        assert store.root.stat().st_mode & 0o777 == 0o700
    assert (checkout / "data").stat().st_mode & 0o777 == 0o755
    custom = LocalSnapshotStore(
        source,
        direct_configuration(
            "genshin", UID, storage_root=tmp_path / "results", auth_root=tmp_path / "auth"
        ),
        "genshin",
    )
    assert custom.root.is_relative_to(tmp_path / "results")
    assert custom.auth_root.is_relative_to(tmp_path / "auth")
    assert (
        stores[0].auth_root
        == LocalSnapshotStore(
            source,
            direct_configuration("genshin", UID, storage_root=tmp_path / "different"),
            "genshin",
        ).auth_root
    )


@pytest.mark.parametrize(
    "kind",
    [
        "auth-repo",
        "auth-other-repo",
        "auth-parent",
        "results-other-repo",
        "results-code",
        "relative-result",
        "relative-auth",
        "auth-symlink",
    ],
)
def test_unsafe_or_relative_roots_are_rejected(checkout, tmp_path, monkeypatch, source, kind):
    monkeypatch.setattr(paths, "project_repository", lambda: checkout)
    result = checkout / "data"
    auth = tmp_path / "auth"
    error = "UNSAFE_AUTH_ROOT"
    if kind == "auth-repo":
        auth = checkout / "private"
    elif kind == "auth-other-repo":
        other = tmp_path / "other"
        other.mkdir()
        git(other, "init", "-q")
        auth = other / "auth"
    elif kind == "auth-parent":
        auth = checkout.parent
    elif kind == "results-other-repo":
        other = tmp_path / "other"
        other.mkdir()
        git(other, "init", "-q")
        result = other / "data"
        error = "UNSAFE_STORAGE_ROOT"
    elif kind == "results-code":
        result = checkout / "src"
        error = "UNSAFE_STORAGE_ROOT"
    elif kind == "relative-result":
        result = Path("data")
        error = "INVALID_STORAGE_ROOT"
    elif kind == "relative-auth":
        auth = Path("auth")
        error = "INVALID_AUTH_ROOT"
    elif kind == "auth-symlink":
        auth.symlink_to(checkout / "data", target_is_directory=True)
    with pytest.raises(WorkspaceError, match=error):
        config = direct_configuration("genshin", UID, storage_root=result, auth_root=auth)
        LocalSnapshotStore(source, config, "genshin")


def test_old_data_and_profile_are_preserved_without_migration(
    checkout, tmp_path, monkeypatch, source
):
    old = tmp_path / "old-private"
    config = direct_configuration("genshin", UID, storage_root=old, auth_root=old)
    store = LocalSnapshotStore(source, config, "genshin")
    with store.locked():
        current = store.prepare()
    store.browser_profile.mkdir(mode=0o700)
    (store.browser_profile / "synthetic-state").write_text("synthetic")
    before = tree_digest(old)
    monkeypatch.setattr(paths, "project_repository", lambda: checkout)
    new = LocalSnapshotStore(source, direct_configuration("genshin", UID, auth_root=old), "genshin")
    assert new.current() is None
    assert new.browser_profile == store.browser_profile
    assert store.current() == current
    assert tree_digest(old) == before
    assert not (new.root / "browser-profile").exists()
    data_digest = tree_digest(current / "data")
    with store.locked():
        assert tree_digest(store.prepare() / "data") == data_digest
    assert (store.browser_profile / "synthetic-state").read_text() == "synthetic"


def test_auth_lock_serializes_different_result_destinations(tmp_path, source):
    a = LocalSnapshotStore(
        source, direct_configuration("genshin", UID, storage_root=tmp_path / "a"), "genshin"
    )
    b = LocalSnapshotStore(
        source, direct_configuration("genshin", UID, storage_root=tmp_path / "b"), "genshin"
    )
    with a.locked():
        with pytest.raises(WorkspaceError, match="USER_WORKSPACE_BUSY"):
            with b.locked():
                pytest.fail("shared auth must be locked")
    with b.locked():
        assert b._locked


def test_local_git_check_rejects_forced_and_already_tracked_paths_without_reading(checkout):
    script = ROOT / "tools/check_private_data.py"
    output = checkout / "data/synthetic-personal.json"
    output.write_text("synthetic private content")
    git(checkout, "add", ".")
    assert git(checkout, "ls-files", "--", "data/").decode().splitlines() == ["data/.gitkeep"]
    assert (
        subprocess.run(
            [sys.executable, str(script), "--root", str(checkout)], capture_output=True
        ).returncode
        == 0
    )
    git(checkout, "add", "-f", "data/synthetic-personal.json")
    output.unlink()  # Missing/unreadable private content is irrelevant: index paths suffice.
    failure = subprocess.run(
        [sys.executable, str(script), "--root", str(checkout)], capture_output=True
    )
    assert failure.returncode == 1
    assert b"TRACKED_DATA_COUNT=1" in failure.stdout
    assert b"synthetic-personal" not in failure.stdout
    assert b"synthetic private content" not in failure.stdout
    nested = checkout / "data/nested"
    nested.mkdir()
    (nested / ".gitkeep").touch()
    git(checkout, "add", "-f", "data/nested/.gitkeep")
    assert (
        subprocess.run(
            [sys.executable, str(script), "--root", str(checkout)], capture_output=True
        ).returncode
        == 1
    )


def test_cli_reports_current_json_location(checkout, monkeypatch, local_tool, capsys):
    monkeypatch.setattr(paths, "project_repository", lambda: checkout)
    assert local_tool.main(["--game", "zzz", "--uid", UID, "--operation", "init"]) == 0
    output = capsys.readouterr().out
    current = next(
        line.removeprefix("CURRENT_RESULT=")
        for line in output.splitlines()
        if line.startswith("CURRENT_RESULT=")
    )
    assert Path(current).is_relative_to(checkout / "data")
    assert (Path(current) / "roster.json").is_file()
    assert (Path(current) / "bangboos.json").is_file()
    assert UID not in output


def test_logout_removes_only_selected_external_auth(tmp_path, source):
    store = LocalSnapshotStore(
        source, direct_configuration("zzz", UID, storage_root=tmp_path / "results"), "zzz"
    )
    other = LocalSnapshotStore(
        source, direct_configuration("zzz", "987654321", storage_root=tmp_path / "results"), "zzz"
    )
    with store.locked():
        current = store.prepare()
    before = tree_digest(current)
    for item in (store, other):
        item.browser_profile.mkdir(mode=0o700)
        (item.browser_profile / "synthetic-state").write_text("synthetic")
        for name in ("identity.key", "verified-identity.json", "request-state.json"):
            (item.auth_root / name).write_text("synthetic")
            (item.auth_root / name).chmod(0o600)
    with store.locked():
        store.logout()
    assert not store.browser_profile.exists()
    assert not (store.auth_root / "identity.key").exists()
    assert not (store.auth_root / "verified-identity.json").exists()
    assert (store.auth_root / "request-state.json").exists()
    assert other.browser_profile.exists()
    assert (other.auth_root / "identity.key").exists()
    assert tree_digest(current) == before


def test_misplaced_legacy_profile_is_rejected_and_preserved(checkout, monkeypatch, source):
    monkeypatch.setattr(paths, "project_repository", lambda: checkout)
    config = direct_configuration("genshin", UID)
    store = LocalSnapshotStore(source, config, "genshin")
    misplaced = store.root / "browser-profile"
    misplaced.mkdir(mode=0o700)
    (misplaced / "synthetic-state").write_text("synthetic")
    before = tree_digest(store.root)
    with pytest.raises(WorkspaceError, match="AUTH_STATE_IN_RESULT_DIRECTORY"):
        LocalSnapshotStore(source, config, "genshin")
    assert tree_digest(store.root) == before
