"""Synthetic ownership controls and interruption boundaries; no real accounts or profiles."""

import importlib
import shutil
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from tests import candidate_policy_for, make_shared
from hoyo_roster import cli, platform_io as io
from hoyo_roster.storage import candidate as storage
from hoyo_roster.storage.common import (
    atomic_write_metadata,
    files_digest,
    load_metadata,
    tree_digest,
)
from hoyo_roster.user_workspace import GAMES, LocalSnapshotStore, direct_configuration
from tests.fixtures import UID_A, UID_B, FakePort


@pytest.fixture
def protocol(tmp_path):
    """Exercise the storage protocol with synthetic public contracts, not game validation."""
    public = tmp_path / "public"
    public.mkdir()
    (public / "contract.json").write_text('{"synthetic": true}\n')
    scope = storage.CandidateScope.identity("genshin-impact", UID_A, "cn_gf01")
    policy = storage.CandidatePolicy(
        owner="synthetic-storage-test",
        candidate_base=tmp_path / "candidates",
        target_relative=Path("data/genshin-impact"),
        contract_paths=(Path("contract.json"),),
        errors=storage.CandidateErrors(ValueError, ValueError, ValueError, ValueError),
        scope=scope,
    )
    profile = {"game": scope.game, "account": {"uid": UID_A, "server": scope.region}}

    def populate(workspace):
        workspace.target_dir.mkdir(parents=True)
        atomic_write_metadata(workspace.target_dir / "profile.json", profile)

    def validate(root):
        assert load_metadata(root / policy.target_relative / "profile.json") == profile

    def evidence(workspace):
        digest = tree_digest(workspace.target_dir)
        return storage.CandidateEvidence(digest, files_digest(public, policy.contract_paths))

    def create(**kwargs):
        return storage.create_candidate_workspace(
            policy, public, populate=populate, validate=validate, evidence=evidence, **kwargs
        )

    return policy, create


@pytest.mark.parametrize("shape", ("empty", "temporary", "unrelated", "legacy-v2"))
def test_uuid_or_temporary_shape_never_proves_ownership(protocol, shape):
    policy, _ = protocol
    io.mkdir_private(policy.candidate_base)
    root = policy.candidate_base / str(uuid.uuid4())
    io.mkdir_private(root)
    if shape == "temporary":
        path = root / f".candidate-state.json.{uuid.uuid4()}.tmp"
        path.write_text("unrelated synthetic evidence")
        path.chmod(0o600)
    elif shape == "unrelated":
        (root / "evidence.txt").write_text("keep")
    elif shape == "legacy-v2":
        atomic_write_metadata(
            root / policy.state_file,
            {
                "authority": "hoyo-repository-mutation/candidate-v2",
                "version": 2,
                "owner": policy.owner,
                "run_id": root.name,
                "status": "building",
            },
        )
    before = tree_digest(root)
    assert storage.inspect_candidate(policy, root).status == "UNOWNED"
    for _ in range(2):
        with pytest.raises(ValueError, match="CANDIDATE_OWNERSHIP_UNPROVEN"):
            storage.cleanup_candidate(policy, root.name)
        assert tree_digest(root) == before


@pytest.mark.parametrize(
    "stage,owned",
    (
        ("after_mkdir", False),
        ("during_state_write", False),
        ("after_state", True),
        ("candidate_validated_metadata_persisted", True),
    ),
)
def test_initialization_interrupts_fail_closed(protocol, stage, owned):
    policy, create = protocol

    def fault(value):
        if value == stage:
            if owned:
                item = storage.inspect_candidates(policy)[0]
                assert item.cleanup_allowed
                assert (
                    load_metadata(policy.candidate_base / item.run_id / policy.state_file)["scope"]
                    == policy.scope.document()
                )
            raise KeyboardInterrupt("synthetic interruption")

    with pytest.raises(KeyboardInterrupt if owned else ValueError):
        create(fault_injector=fault)
    items = storage.inspect_candidates(policy)
    if owned:
        assert items == ()
    else:
        assert len(items) == 1 and items[0].status == "UNOWNED" and not items[0].cleanup_allowed


@pytest.mark.parametrize(
    "mutation,status",
    (
        ("project", "UNOWNED"),
        ("game", "SCOPE_MISMATCH"),
        ("uid", "SCOPE_MISMATCH"),
        ("region", "SCOPE_MISMATCH"),
        ("profile", "SCOPE_MISMATCH"),
        ("extra", "UNOWNED"),
        ("contract", "UNOWNED"),
        ("mode", "UNOWNED"),
        ("symlink", "UNOWNED"),
        ("missing-marker", "UNOWNED"),
        ("no-selector", "UNOWNED"),
        ("invalid-scope-type", "UNOWNED"),
    ),
)
def test_candidate_scope_content_and_permissions_are_rechecked(protocol, mutation, status):
    policy, create = protocol
    workspace = create()
    marker = workspace.root / policy.state_file
    state = load_metadata(marker)
    selected = policy
    if mutation in {"project", "game", "uid", "region", "invalid-scope-type"}:
        field, value = {
            "project": ("project", "foreign-project"),
            "game": ("game", "zenless-zone-zero"),
            "uid": ("uid_digest", "0" * 64),
            "region": ("region", "cn_qd01"),
            "invalid-scope-type": ("uid_digest", None),
        }[mutation]
        state["scope"][field] = value
        if mutation == "game":
            state["scope"]["region"] = "prod_gf_cn"
        atomic_write_metadata(marker, state)
    elif mutation == "profile":
        atomic_write_metadata(
            workspace.target_dir / "profile.json",
            {
                "game": policy.scope.game,
                "account": {"uid": UID_B, "server": "cn_gf01"},
            },
        )
    elif mutation == "extra":
        atomic_write_metadata(workspace.root / "unrelated.json", {"synthetic": True})
    elif mutation == "contract":
        atomic_write_metadata(workspace.root / "contract.json", {"changed": True})
    elif mutation == "mode":
        make_shared(marker, 0o644)
    elif mutation == "symlink":
        (workspace.target_dir / "foreign-link").symlink_to(workspace.root.parent)
    elif mutation == "missing-marker":
        marker.unlink()
    elif mutation == "no-selector":
        selected = replace(policy, scope=None)
    assert storage.inspect_candidate(selected, workspace.root).status == status
    with pytest.raises(ValueError, match="CANDIDATE_"):
        storage.cleanup_candidate(selected, workspace.root.name)
    assert workspace.root.exists()


def test_partial_candidate_cleanup_without_marker_requires_manual_handling(protocol, monkeypatch):
    policy, create = protocol
    workspace = create()

    def partial(root):
        (root / policy.state_file).unlink()
        raise OSError("synthetic partial cleanup")

    monkeypatch.setattr(storage, "remove_owned_tree", partial)
    with pytest.raises(ValueError, match="candidate cleanup failed"):
        workspace.cleanup()
    monkeypatch.undo()
    digest = tree_digest(workspace.root)
    for _ in range(2):
        assert not storage.inspect_candidate(policy, workspace.root).cleanup_allowed
        with pytest.raises(ValueError, match="OWNERSHIP_UNPROVEN"):
            workspace.cleanup()
        assert tree_digest(workspace.root) == digest


@pytest.mark.parametrize("game", ("genshin", "zzz"))
def test_real_creation_binds_scope_and_cli_rejects_foreign_copy(
    tmp_path, monkeypatch, capsys, game
):
    """Run real providers/collectors/normalizers and inject only browser/network and a fault."""
    domain = importlib.import_module(f"hoyo_roster.games.{game}.cli")
    module = importlib.import_module(f"hoyo_roster.games.{game}.candidate")
    root = tmp_path / "private"
    store = LocalSnapshotStore(
        cli.CONTRACT_ROOT,
        direct_configuration(game, UID_A, storage_root=root),
        game,
        candidate_policy=candidate_policy_for(game),
    )
    with store.locked():
        current = store.prepare()
    before_data = tree_digest(current / "data")
    port = FakePort()
    seen = []
    original_create = domain.create_candidate

    def fault(stage):
        if stage == "after_state":
            marker = load_metadata(next(store.candidate_base.iterdir()) / "candidate-state.json")
            assert marker["status"] == "building"
            seen.append(marker["scope"])
        if stage == "candidate_validated_metadata_persisted":
            raise module.CandidateValidationError("synthetic interruption after validation")

    def create(*args, **kwargs):
        return original_create(*args, fault_injector=fault, **kwargs)

    monkeypatch.setattr(domain, "create_candidate", create)
    monkeypatch.setattr(
        storage,
        "remove_owned_tree",
        lambda path: (_ for _ in ()).throw(OSError("synthetic cleanup failure")),
    )
    argv = ["--game", game, "--uid", UID_A, "--storage-root", str(root)]
    assert cli.main(argv, client=port.client()) == 1
    monkeypatch.undo()
    # The normal CLI prepare upgrades the baseline before collecting. Pin the generation
    # that is current at the injected candidate failure, then prove cleanup preserves it.
    current = store.current()
    assert tree_digest(current / "data") == before_data
    digest = tree_digest(current)
    item = module.inspect_candidates(candidate_base=store.candidate_base, uid=UID_A)[0]
    assert item.status == "VALIDATED" and item.cleanup_allowed
    candidate = store.candidate_base / item.run_id
    marker = load_metadata(candidate / "candidate-state.json")
    assert marker["scope"] == seen[0]
    assert (
        marker["scope"]
        == storage.CandidateScope.identity(
            GAMES[game], UID_A, "cn_gf01" if game == "genshin" else "prod_gf_cn"
        ).document()
    )
    assert all(io.private(p) for p in (candidate, *candidate.rglob("*")))
    foreign_store = LocalSnapshotStore(
        cli.CONTRACT_ROOT,
        direct_configuration(game, UID_B, storage_root=root),
        game,
        candidate_policy=candidate_policy_for(game),
    )
    with foreign_store.locked():
        foreign_current = foreign_store.prepare()
    io.mkdir_private(foreign_store.candidate_base)
    foreign = foreign_store.candidate_base / item.run_id
    shutil.copytree(candidate, foreign)
    before = tree_digest(foreign)
    foreign_args = ["--game", game, "--uid", UID_B, "--storage-root", str(root)]
    assert cli.main([*foreign_args, "--operation", "inspect"]) == 0
    assert "CANDIDATE_STATUS=SCOPE_MISMATCH" in capsys.readouterr().out
    for _ in range(2):
        assert (
            cli.main([*foreign_args, "--operation", "cleanup-candidate", "--run-id", item.run_id])
            == 1
        )
        assert tree_digest(foreign) == before and foreign_store.current() == foreign_current
    assert cli.main([*argv, "--operation", "cleanup-candidate", "--run-id", item.run_id]) == 0
    assert not candidate.exists() and foreign.exists()
    assert store.current() == current and tree_digest(current) == digest
    output = capsys.readouterr().out
    diagnostics = "\n".join(
        line
        for line in output.splitlines()
        if not line.startswith(("RESULT_SCOPE=", "CURRENT_POINTER=", "CURRENT_RESULT="))
    )
    assert UID_A not in output and UID_B not in output and str(root) not in diagnostics
