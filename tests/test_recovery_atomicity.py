"""Whole-operation recovery controls through real APIs and CLI, with fresh synthetic roots."""

import os
import shutil
import stat
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from tests import candidate_policy_for
from hoyo_roster import cli, user_workspace as uw
from hoyo_roster.storage.common import atomic_write_metadata, load_metadata, tree_digest
from tests.fixtures import UID_A


def make_store(root, game="genshin"):
    store = uw.LocalSnapshotStore(
        cli.CONTRACT_ROOT,
        uw.direct_configuration(game, UID_A, storage_root=root),
        game,
        candidate_policy=candidate_policy_for(game),
    )
    with store.locked():
        store._install(None)
    return store


def invoke(store, operation, *extra):
    key = next(key for key, game in uw.GAMES.items() if game == store.game)
    return cli.main(
        [
            "--game",
            key,
            "--uid",
            UID_A,
            "--storage-root",
            str(store.root.parents[3]),
            "--operation",
            operation,
            *extra,
        ]
    )


def pending(store, partial=False):
    old = store.current()
    remove = uw.remove_owned_tree

    def fail(path):
        if path == old:
            if partial:
                (path / "generation.json").unlink()
                (path / "data" / store.game / "profile.json").unlink()
            raise OSError("synthetic cleanup failure")
        return remove(path)

    with store.locked(), patch.object(uw, "remove_owned_tree", side_effect=fail):
        new = store._install(None)
    assert store.commit_state == "COMMITTED" and store.cleanup_pending
    return old, new


def genshin_candidate(store, region="cn_gf01"):
    from hoyo_roster.games.genshin.normalize import normalize_snapshot
    from hoyo_roster.games.genshin.candidate import create_candidate

    snapshot = normalize_snapshot(
        {"uid": UID_A, "server": region, "nickname": "Synthetic", "player_level": 20},
        [
            {
                "id": 10000005,
                "name": "Synthetic Traveler",
                "avatar_level": 5,
                "level_current": 20,
                "promote_level": 1,
                "constellation_num": 0,
                "fetter_level": 1,
                "element_attr_id": 3,
                "weapon": None,
                "skill_list": [
                    {"id": 100545, "group_id": 730, "level_current": 1, "max_level": 10},
                    {"id": 10077, "group_id": 932, "level_current": 1, "max_level": 10},
                    {"id": 10078, "group_id": 939, "level_current": 1, "max_level": 10},
                ],
            }
        ],
    )
    return create_candidate(
        store.current(), snapshot, candidate_base=store.candidate_base, uid=UID_A
    )


@pytest.mark.parametrize("game", uw.GAMES)
@pytest.mark.parametrize("explicit", (False, True))
@pytest.mark.parametrize(
    "damage",
    (
        "json",
        "authority",
        "scope",
        "identity",
        "empty",
        "missing-new",
        "active-hash",
        "old-hash",
        "path",
        "absent-old-inventory",
        "duplicate-reference",
        "foreign-reference",
        "null-new",
        "missing-marker",
        "missing-inventory",
    ),
)
def test_entire_journal_preflight_preserves_all_state(tmp_path, capsys, game, explicit, damage):
    store = make_store(tmp_path / "private", game)
    old, new = pending(store)
    value = load_metadata(store.transaction_file)
    if damage == "json":
        store.transaction_file.write_text("{broken")
    else:
        if damage == "authority":
            value["authority"] = "unowned"
        elif damage == "scope":
            value["scope"] = "0" * 64
        elif damage == "identity":
            value["identity"]["region"] = "foreign"
        elif damage == "empty":
            value["inventories"] = {}
        elif damage == "missing-new":
            del value["inventories"][new.name]
        elif damage == "active-hash":
            value["inventories"][new.name]["data/manifest.json"] = "0" * 64
        elif damage == "old-hash":
            value["inventories"][old.name]["data/manifest.json"] = "0" * 64
        elif damage == "path":
            value["inventories"][old.name]["../foreign"] = "directory"
        elif damage == "absent-old-inventory":
            shutil.rmtree(old)
            value["inventories"][old.name] = {"foreign": "0" * 64}
        elif damage == "duplicate-reference":
            value["old"] = value["new"]
        elif damage == "foreign-reference":
            value["new"] = str(uuid.uuid4())
        elif damage == "null-new":
            value["new"] = None
        elif damage == "missing-marker":
            del value["inventories"][new.name]["generation.json"]
        elif damage == "missing-inventory":
            del value["inventories"]
        atomic_write_metadata(store.transaction_file, value)
    before = tree_digest(store.root)
    extra = ("--generation", old.name) if explicit else ()
    assert invoke(store, "recover", *extra) == 1
    output = capsys.readouterr().out
    assert "COMMIT_STATE=NOT_COMMITTED" in output
    diagnostics = "\n".join(
        line
        for line in output.splitlines()
        if not line.startswith(("RESULT_SCOPE=", "CURRENT_POINTER=", "CURRENT_RESULT="))
    )
    assert f"RESULT_SCOPE={store.root}" in output
    assert f"CURRENT_POINTER={store.current_file}" in output
    assert UID_A not in output and str(tmp_path) not in diagnostics
    assert tree_digest(store.root) == before


@pytest.mark.parametrize("game", uw.GAMES)
@pytest.mark.parametrize("explicit", (False, True))
@pytest.mark.parametrize("partial", (False, True))
def test_valid_and_repeated_recovery(tmp_path, capsys, game, explicit, partial):
    store = make_store(tmp_path / "private", game)
    old, new = pending(store, partial)
    # A partial residue can be cleaned, but can never be activated.
    selected = new if partial else old
    extra = ("--generation", selected.name) if explicit else ()
    kept = selected if explicit else new
    digest = tree_digest(kept)
    for _ in range(2):
        assert invoke(store, "recover", *extra) == 0
        assert store.current() == kept and tree_digest(kept) == digest
        assert not store.transaction_file.exists()
    assert "COMMIT_STATE=COMMITTED" in capsys.readouterr().out


def test_explicit_bad_inventory_does_not_roll_back_partial_data(tmp_path, capsys):
    store = make_store(tmp_path / "private")
    old = store.current()
    candidate = genshin_candidate(store)
    remove = uw.remove_owned_tree

    def fail(path):
        if path == old:
            raise OSError("synthetic cleanup")
        return remove(path)

    with store.locked(), patch.object(uw, "remove_owned_tree", side_effect=fail):
        store.commit(candidate.game_dir)
    new = store.current()
    assert load_metadata(new / "data" / store.game / "profile.json")["sync"]["state"] == "partial"
    value = load_metadata(store.transaction_file)
    value["inventories"] = {}
    atomic_write_metadata(store.transaction_file, value)
    before = tree_digest(store.root)
    assert invoke(store, "recover", "--generation", old.name) == 1
    assert "UNOWNED_TRANSACTION" in capsys.readouterr().out
    assert tree_digest(store.root) == before and store.current() == new


@pytest.mark.parametrize("damage", ("extra", "json", "missing", "unknown-generation"))
def test_corrupt_current_cannot_relax_region(tmp_path, capsys, damage):
    store = make_store(tmp_path / "recipient")
    own = genshin_candidate(store)
    with store.locked():
        store.commit(own.game_dir)
    own.cleanup()
    donor = make_store(tmp_path / "donor")
    foreign = genshin_candidate(donor, "cn_qd01")
    copy = store.candidate_base / foreign.root.name
    shutil.copytree(foreign.root, copy)
    assert invoke(store, "cleanup-candidate", "--run-id", copy.name) == 1
    current = store.current()
    if damage == "extra":
        atomic_write_metadata(store.current_file, {"generation": current.name, "unexpected": True})
    elif damage == "json":
        store.current_file.write_text("{broken")
    elif damage == "missing":
        store.current_file.unlink()
    else:
        atomic_write_metadata(store.current_file, {"generation": str(uuid.uuid4())})
    before = tree_digest(store.root)
    assert invoke(store, "inspect") == 0
    assert "CANDIDATE_CLEANUP_ALLOWED=NO" in capsys.readouterr().out
    for _ in range(2):
        assert invoke(store, "cleanup-candidate", "--run-id", copy.name) == 1
        assert "CANDIDATE_SCOPE_MISMATCH" in capsys.readouterr().out
        assert tree_digest(store.root) == before
    # Explicit activation uses the same authoritative region and keeps the boundary.
    assert invoke(store, "recover", "--generation", current.name) == 0
    assert store.candidate_region == "cn_gf01" and foreign.root.exists()


@pytest.mark.parametrize("damage", ("missing", "json", "scope", "game", "uid", "region", "unbound"))
def test_missing_or_conflicting_identity_blocks_recovery_and_cleanup(tmp_path, capsys, damage):
    store = make_store(tmp_path / "private")
    own = genshin_candidate(store)
    with store.locked():
        store.commit(own.game_dir)
    own.cleanup()
    candidate = genshin_candidate(store)
    current = store.current()
    value = load_metadata(store.identity_file)
    if damage == "missing":
        store.identity_file.unlink()
    elif damage == "json":
        store.identity_file.write_text("{broken")
    else:
        key = {"uid": "uid_digest", "unbound": "region"}.get(damage, damage)
        value[key] = None if damage == "unbound" else "foreign"
        atomic_write_metadata(store.identity_file, value)
    before = tree_digest(store.root)
    for op, args in (
        ("recover", ("--generation", current.name)),
        ("recover", ()),
        ("cleanup-generation", ("--generation", current.name)),
        ("cleanup-candidate", ("--run-id", candidate.root.name)),
        ("init", ()),
    ):
        assert invoke(store, op, *args) == 1
        assert tree_digest(store.root) == before
    assert invoke(store, "inspect") == 0
    assert "CANDIDATE_CLEANUP_ALLOWED=NO" in capsys.readouterr().out


@pytest.mark.parametrize("damage", ("marker", "profile", "roster", "contract", "permissions"))
def test_activation_validation_never_changes_current(tmp_path, damage):
    store = make_store(tmp_path / "private")
    old, new = pending(store)
    if damage == "marker":
        (old / "generation.json").unlink()
    elif damage == "permissions":
        old.chmod(0o755)
    else:
        path = (
            old
            / {
                "profile": f"data/{store.game}/profile.json",
                "roster": f"data/{store.game}/roster.json",
                "contract": "tools/validate_repository.py",
            }[damage]
        )
        path.write_text("{}")
    before = tree_digest(store.root)
    assert invoke(store, "recover", "--generation", old.name) == 1
    assert tree_digest(store.root) == before and store.current() == new


@pytest.mark.parametrize("explicit", (False, True))
@pytest.mark.parametrize("journal", (False, True))
@pytest.mark.parametrize(
    "fault", ("file-fsync", "replace", "parent-fsync", "cleanup", "unlink", "cleanup-fsync")
)
def test_real_recovery_commit_boundaries(tmp_path, monkeypatch, capsys, explicit, journal, fault):
    if not explicit and fault in ("file-fsync", "replace"):
        pytest.skip("implicit confirmation performs no file write/replace")
    if not journal and fault in ("cleanup", "unlink", "cleanup-fsync"):
        pytest.skip("no journal means no cleanup phase")
    store = make_store(tmp_path / "private")
    old, new = pending(store)
    if not journal:
        store.transaction_file.unlink()
    before = {p: tree_digest(p) for p in (old, new)}
    pointer = store.current_file.read_bytes()
    original_replace, original_fsync = os.replace, os.fsync
    original_remove, original_unlink = uw.remove_owned_tree, Path.unlink
    replaced = False
    unlinked = False
    root_stat = store.root.stat()

    def replace(src, dst):
        nonlocal replaced
        if Path(dst) == store.current_file:
            if fault == "replace":
                raise OSError("synthetic pre-replace")
            result = original_replace(src, dst)
            replaced = True
            return result
        return original_replace(src, dst)

    def fsync(fd):
        meta = os.fstat(fd)
        parent = stat.S_ISDIR(meta.st_mode) and (meta.st_dev, meta.st_ino) == (
            root_stat.st_dev,
            root_stat.st_ino,
        )
        if (
            (fault == "file-fsync" and not stat.S_ISDIR(meta.st_mode))
            or (fault == "parent-fsync" and parent and (replaced or not explicit))
            or (fault == "cleanup-fsync" and parent and unlinked)
        ):
            raise OSError("synthetic fsync")
        return original_fsync(fd)

    def remove(path):
        if fault == "cleanup":
            raise OSError("synthetic cleanup")
        return original_remove(path)

    def unlink(path, *args, **kwargs):
        nonlocal unlinked
        if path == store.transaction_file:
            if fault == "unlink":
                raise OSError("synthetic unlink")
            unlinked = True
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as hooks:
        hooks.setattr(os, "replace", replace)
        hooks.setattr(os, "fsync", fsync)
        hooks.setattr(uw, "remove_owned_tree", remove)
        hooks.setattr(Path, "unlink", unlink)
        result = invoke(store, "recover", *(("--generation", old.name) if explicit else ()))
    output = capsys.readouterr().out
    if fault in ("file-fsync", "replace", "parent-fsync"):
        assert result == 1
        state = "DURABILITY_UNKNOWN" if fault == "parent-fsync" else "NOT_COMMITTED"
        assert f"COMMIT_STATE={state}" in output
        assert all(tree_digest(p) == digest for p, digest in before.items())
        if state == "NOT_COMMITTED":
            assert store.current_file.read_bytes() == pointer
        assert store.transaction_file.exists() == journal
    else:
        assert result == 0 and "COMMIT_STATE=COMMITTED" in output and "CLEANUP_PENDING" in output
    kept = old if explicit and fault not in ("file-fsync", "replace") else new
    assert store.current() == kept
    for _ in range(2):
        assert invoke(store, "recover") == 0
        assert store.current() == kept and tree_digest(kept) == before[kept]


@pytest.mark.parametrize("damage", ("marker", "profile", "contract", "permissions", "region"))
def test_commit_preflight_retains_rejected_candidate(tmp_path, damage):
    store = make_store(tmp_path / "private")
    candidate = genshin_candidate(store)
    if damage == "marker":
        (candidate.root / "candidate-state.json").unlink()
    elif damage == "permissions":
        candidate.root.chmod(0o755)
    elif damage == "region":
        atomic_write_metadata(store.identity_file, store._identity_document("cn_qd01"))
    else:
        path = (
            candidate.root
            / {
                "profile": f"data/{store.game}/profile.json",
                "contract": "tools/validate_repository.py",
            }[damage]
        )
        path.write_text("{}")
    before = tree_digest(store.root)
    with store.locked(), pytest.raises(uw.WorkspaceError):
        store.commit(candidate.game_dir)
    assert tree_digest(store.root) == before and store.commit_state == "NOT_COMMITTED"


@pytest.mark.parametrize("current_damage", (False, True))
def test_cross_region_generation_cannot_activate_or_delete(tmp_path, current_damage):
    recipient = make_store(tmp_path / "recipient")
    own = genshin_candidate(recipient)
    with recipient.locked():
        recipient.commit(own.game_dir)
    own.cleanup()
    donor = make_store(tmp_path / "donor")
    other = genshin_candidate(donor, "cn_qd01")
    with donor.locked():
        donor.commit(other.game_dir)
    foreign = donor.current()
    copy = recipient.root / foreign.name
    shutil.copytree(foreign, copy)
    if current_damage:
        recipient.current_file.write_text("{broken")
    before = tree_digest(recipient.root)
    for op in ("recover", "cleanup-generation"):
        assert invoke(recipient, op, "--generation", copy.name) == 1
        assert tree_digest(recipient.root) == before


@pytest.mark.parametrize("stage", ("identity", "journal", "pointer"))
@pytest.mark.parametrize("fault", ("file-fsync", "replace", "parent-fsync"))
def test_install_metadata_boundaries_preserve_needed_generations(
    tmp_path, monkeypatch, stage, fault
):
    store = make_store(tmp_path / "private")
    old = store.current()
    before = tree_digest(old)
    candidate = genshin_candidate(store)
    candidate_before = tree_digest(candidate.root)
    selected = {
        "identity": store.identity_file,
        "journal": store.transaction_file,
        "pointer": store.current_file,
    }[stage]
    original_atomic = uw.atomic_write_metadata
    original_replace, original_fsync = os.replace, os.fsync

    def atomic(path, value):
        if path != selected:
            return original_atomic(path, value)
        parent = path.parent.stat()

        def replace(src, dst):
            if fault == "replace":
                raise OSError("synthetic metadata replace")
            return original_replace(src, dst)

        def fsync(fd):
            meta = os.fstat(fd)
            isdir = stat.S_ISDIR(meta.st_mode)
            if (fault == "file-fsync" and not isdir) or (
                fault == "parent-fsync"
                and isdir
                and (meta.st_dev, meta.st_ino) == (parent.st_dev, parent.st_ino)
            ):
                raise OSError("synthetic metadata fsync")
            return original_fsync(fd)

        with monkeypatch.context() as hooks:
            hooks.setattr(os, "replace", replace)
            hooks.setattr(os, "fsync", fsync)
            return original_atomic(path, value)

    with store.locked(), monkeypatch.context() as hooks:
        hooks.setattr(uw, "atomic_write_metadata", atomic)
        with pytest.raises((OSError, uw.WorkspaceError)):
            store.commit(candidate.game_dir)
    assert tree_digest(old) == before and tree_digest(candidate.root) == candidate_before
    state = (
        "DURABILITY_UNKNOWN" if stage == "pointer" and fault == "parent-fsync" else "NOT_COMMITTED"
    )
    assert store.commit_state == state
    if state == "NOT_COMMITTED":
        assert store.current() == old
    if store.transaction_file.exists():
        journal = load_metadata(store.transaction_file)
        assert all((store.root / journal[key]).exists() for key in ("old", "new"))
        with store.locked():
            store.recover()
    assert candidate.root.exists()
