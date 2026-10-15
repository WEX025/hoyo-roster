"""Actual replace/fsync/cleanup fault boundaries and ownership-aware recovery."""

import os
import stat
import uuid
import pytest
import hoyo_roster.user_workspace as workspace
from tests import candidate_policy_for
from hoyo_roster.storage.common import (
    load_metadata,
    tree_digest,
    atomic_write_metadata,
    AtomicWriteError,
)
from tests.test_login_flow import UID_A


@pytest.fixture
def store(tmp_path, source):
    value = workspace.LocalSnapshotStore(
        source,
        workspace.direct_configuration("genshin", UID_A, storage_root=tmp_path / "private"),
        "genshin",
        candidate_policy=candidate_policy_for("genshin"),
    )
    with value.locked():
        yield value


@pytest.mark.parametrize("stage", ("file_fsync", "before_replace", "parent_fsync"))
def test_atomic_failure_explicit_phase(tmp_path, monkeypatch, stage):
    target = tmp_path / "state.json"
    atomic_write_metadata(target, {"old": True})
    original_fsync = os.fsync

    def fsync(fd):
        isdir = stat.S_ISDIR(os.fstat(fd).st_mode)
        if (stage == "file_fsync" and not isdir) or (stage == "parent_fsync" and isdir):
            raise OSError("synthetic")
        return original_fsync(fd)

    def before_replace(path):
        if stage == "before_replace":
            raise OSError("synthetic")

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(AtomicWriteError if stage == "parent_fsync" else OSError):
        atomic_write_metadata(target, {"new": True}, before_replace=before_replace)
    assert load_metadata(target) == ({"new": True} if stage == "parent_fsync" else {"old": True})
    assert list(tmp_path.iterdir()) == [target]


def test_pointer_post_replace_fsync_retains_both_and_recovers(store, monkeypatch):
    old = store._install(None)
    old_digest = tree_digest(old / "data")
    original = workspace.atomic_write_metadata

    def injected(path, value):
        if path != store.current_file:
            return original(path, value)
        original_fsync = os.fsync
        parent = path.parent.stat()

        def fsync(fd):
            meta = os.fstat(fd)
            if stat.S_ISDIR(meta.st_mode) and (meta.st_dev, meta.st_ino) == (
                parent.st_dev,
                parent.st_ino,
            ):
                raise OSError("synthetic parent fsync")
            return original_fsync(fd)

        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", fsync)
            return original(path, value)

    monkeypatch.setattr(workspace, "atomic_write_metadata", injected)
    with pytest.raises(workspace.WorkspaceError, match="COMMIT_DURABILITY_UNKNOWN"):
        store._install(None)
    assert store.commit_state == "DURABILITY_UNKNOWN"
    new = store.current()
    assert new != old and new.exists() and old.exists()
    assert tree_digest(new / "data") == old_digest
    assert store.inspect()["recovery_required"]
    monkeypatch.setattr(workspace, "atomic_write_metadata", original)
    assert store.recover() == new and not old.exists()
    store._install(None)


def test_cleanup_failure_is_success_with_recoverable_pending(store, monkeypatch):
    old = store._install(None)
    remove = workspace.remove_owned_tree

    def injected(path):
        if path == old:
            raise OSError("synthetic cleanup")
        return remove(path)

    monkeypatch.setattr(workspace, "remove_owned_tree", injected)
    new = store._install(None)
    assert store.commit_state == "COMMITTED" and store.cleanup_pending and store.current() == new
    assert old.exists() and new.exists()
    monkeypatch.setattr(workspace, "remove_owned_tree", remove)
    assert store.recover() == new and not old.exists() and not store.cleanup_pending
    store._install(None)


@pytest.mark.parametrize(
    "stage", ("validation", "data_fsync", "journal_replace", "pointer_replace")
)
def test_precommit_fault_preserves_old(store, monkeypatch, stage):
    old = store._install(None)
    digest = tree_digest(old / "data")
    original_atomic = workspace.atomic_write_metadata
    if stage == "validation":
        monkeypatch.setattr(
            workspace,
            "validate_workspace",
            lambda root, game=None: (_ for _ in ()).throw(workspace.WorkspaceError("synthetic")),
        )
    elif stage == "data_fsync":
        monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("synthetic")))
    else:

        def injected(path, value):
            target = store.transaction_file if stage == "journal_replace" else store.current_file
            if path == target:
                raise OSError("synthetic before replace")
            return original_atomic(path, value)

        monkeypatch.setattr(workspace, "atomic_write_metadata", injected)
    with pytest.raises((OSError, workspace.WorkspaceError)):
        store._install(None)
    monkeypatch.undo()
    assert store.current() == old and tree_digest(old / "data") == digest
    assert store.commit_state == "NOT_COMMITTED"
    if store.transaction_file.exists():
        store.recover()
    assert store.current() == old


def test_invalid_pointer_inspect_and_verified_explicit_recovery(store):
    old = store._install(None)
    atomic_write_metadata(store.current_file, {"generation": str(uuid.uuid4())})
    assert store.inspect()["current"] == "INVALID"
    with pytest.raises(workspace.WorkspaceError, match="RECOVERY_GENERATION_REQUIRED"):
        store.recover()
    assert store.recover(old.name) == old


def test_uuid_is_not_cleanup_or_recovery_authority(store):
    current = store._install(None)
    unowned = store.root / str(uuid.uuid4())
    unowned.mkdir(mode=0o700)
    with pytest.raises((workspace.WorkspaceError, ValueError)):
        store.recover(unowned.name)
    assert unowned.exists() and store.current() == current
    with pytest.raises(workspace.WorkspaceError, match="RESIDUE"):
        store._install(None)


def test_wrong_identity_or_corrupted_generation_recovery_rejected(store):
    old = store._install(None)
    profile = old / "data" / store.game / "profile.json"
    state = load_metadata(profile)
    state["account"]["uid"] = "900000002"
    workspace.write_json(profile, state)
    assert store.inspect()["generations"][0]["status"] == "UNVERIFIED"
    with pytest.raises(workspace.WorkspaceError):
        store.recover(old.name)
    assert old.exists()


def test_new_intermediates_private_existing_shared_parent_unchanged(tmp_path):
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    leaf = workspace.private_directory(parent / "application" / "users" / "scope")
    assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    for path in (leaf, leaf.parent, leaf.parent.parent):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    with pytest.raises(workspace.WorkspaceError):
        workspace.private_directory(parent)
    assert stat.S_IMODE(parent.stat().st_mode) == 0o755


def test_symlink_rejected_without_touching_target(tmp_path):
    target = tmp_path / "shared"
    target.mkdir()
    target.chmod(0o755)
    (tmp_path / "link").symlink_to(target)
    with pytest.raises(workspace.WorkspaceError):
        workspace.private_directory(tmp_path / "link" / "private")
    assert not (target / "private").exists()


def test_partial_cleanup_can_resume_from_validated_inventory(store, monkeypatch):
    old = store._install(None)
    remove = workspace.remove_owned_tree

    def partial(path):
        if path == old:
            (path / "data" / store.game / "profile.json").unlink()
            (path / "generation.json").unlink()
            raise OSError("synthetic partially deleted old generation")
        return remove(path)

    monkeypatch.setattr(workspace, "remove_owned_tree", partial)
    new = store._install(None)
    assert store.cleanup_pending and store.current() == new
    monkeypatch.setattr(workspace, "remove_owned_tree", remove)
    assert store.recover() == new and not old.exists()
    store._install(None)


def test_changed_partial_cleanup_is_retained(store, monkeypatch):
    old = store._install(None)
    remove = workspace.remove_owned_tree

    def failed(path):
        if path == old:
            raise OSError("synthetic")
        return remove(path)

    monkeypatch.setattr(workspace, "remove_owned_tree", failed)
    new = store._install(None)
    monkeypatch.setattr(workspace, "remove_owned_tree", remove)
    (old / "unknown.json").write_text("{}")
    (old / "unknown.json").chmod(0o600)
    with pytest.raises(workspace.WorkspaceError, match="CLEANUP_RESIDUE_CHANGED"):
        store.recover()
    assert old.exists() and store.current() == new


def test_explicit_cleanup_never_deletes_current_or_unowned(store):
    current = store._install(None)
    with pytest.raises(workspace.WorkspaceError, match="CURRENT_GENERATION_PROTECTED"):
        store.cleanup_generation(current.name)
    unowned = store.root / str(uuid.uuid4())
    unowned.mkdir(mode=0o700)
    with pytest.raises((workspace.WorkspaceError, ValueError)):
        store.cleanup_generation(unowned.name)
    assert unowned.exists() and store.current() == current
