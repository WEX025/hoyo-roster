from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import candidate_policy_for
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
    assert paths.default_storage_root().expanduser() == paths.default_private_root() / "results"


def test_explicit_results_and_shared_runtime_roots(checkout, tmp_path, monkeypatch, source):
    from hoyo_roster.auth import runtime_scope

    monkeypatch.setattr(paths, "project_repository", lambda: checkout)
    stores = [
        LocalSnapshotStore(
            source,
            direct_configuration(game, uid),
            game,
            candidate_policy=candidate_policy_for(game),
        )
        for game in ("genshin", "zzz")
        for uid in (UID, "987654321")
    ]
    assert len({store.root for store in stores}) == 4
    assert not paths.default_private_root().exists()
    for store in stores:
        assert store.root.is_relative_to(checkout / "data/game-results-v3")
        assert store.root.stat().st_mode & 0o777 == 0o700
    shared = runtime_scope(tmp_path / "auth")
    assert shared == runtime_scope(tmp_path / "auth")
    assert not shared.is_relative_to(checkout)
    assert (checkout / "data").stat().st_mode & 0o777 == 0o755


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
    error = "UNSAFE_RUNTIME_ROOT"
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
        error = "INVALID_RUNTIME_ROOT"
    elif kind == "auth-symlink":
        error = "UNSAFE_PRIVATE_DIRECTORY"
        auth.symlink_to(checkout / "data", target_is_directory=True)
    with pytest.raises(WorkspaceError, match=error):
        if kind.startswith("auth-") or kind == "relative-auth":
            from hoyo_roster.auth import runtime_scope

            runtime_scope(auth)
        config = direct_configuration("genshin", UID, storage_root=result)
        LocalSnapshotStore(
            source, config, "genshin", candidate_policy=candidate_policy_for("genshin")
        )


def test_old_data_and_profile_are_preserved_without_migration(
    checkout, tmp_path, monkeypatch, source
):
    from hoyo_roster.auth import runtime_scope
    from hoyo_roster.storage.common import secure_private_directory

    old = secure_private_directory(tmp_path / "auth/users/legacy/genshin")
    (old / "synthetic-state").write_text("synthetic")
    before = tree_digest(old)
    monkeypatch.setattr(paths, "project_repository", lambda: checkout)
    store = LocalSnapshotStore(
        source,
        direct_configuration("genshin", UID),
        "genshin",
        candidate_policy=candidate_policy_for("genshin"),
    )
    assert store.current() is None
    runtime_scope(tmp_path / "auth")
    with store.locked():
        store.prepare()
    assert tree_digest(old) == before
    assert not (store.root / "browser-profile").exists()


def test_result_locks_are_independent_of_other_destinations(tmp_path, source):
    stores = [
        LocalSnapshotStore(
            source,
            direct_configuration("genshin", UID, storage_root=tmp_path / name),
            "genshin",
            candidate_policy=candidate_policy_for("genshin"),
        )
        for name in ("a", "b")
    ]
    with stores[0].locked(), stores[1].locked():
        assert all(store._locked for store in stores)


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


def test_misplaced_legacy_profile_is_rejected_and_preserved(checkout, monkeypatch, source):
    monkeypatch.setattr(paths, "project_repository", lambda: checkout)
    config = direct_configuration("genshin", UID)
    store = LocalSnapshotStore(
        source, config, "genshin", candidate_policy=candidate_policy_for("genshin")
    )
    misplaced = store.root / "browser-profile"
    misplaced.mkdir(mode=0o700)
    (misplaced / "synthetic-state").write_text("synthetic")
    before = tree_digest(store.root)
    with pytest.raises(WorkspaceError, match="AUTH_STATE_IN_RESULT_DIRECTORY"):
        LocalSnapshotStore(
            source, config, "genshin", candidate_policy=candidate_policy_for("genshin")
        )
    assert tree_digest(store.root) == before
