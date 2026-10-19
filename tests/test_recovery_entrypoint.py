"""Recovery commands run through the public parser, with synthetic generations only."""

import uuid

import pytest

from tests import candidate_policy_for
from hoyo_roster import cli, user_workspace
from hoyo_roster.storage.common import tree_digest
from tests.fixtures import UID_A


@pytest.mark.parametrize(
    "arguments,code",
    (
        (["--operation", "cleanup-generation"], "GENERATION_REQUIRED"),
        (
            ["--operation", "cleanup-generation", "--generation", "sensitive-invalid-value"],
            "INVALID_GENERATION",
        ),
        (
            ["--operation", "recover", "--generation", "../sensitive-invalid-value"],
            "INVALID_GENERATION",
        ),
        (["--operation", "cleanup-candidate"], "CANDIDATE_RUN_ID_REQUIRED"),
        (
            ["--operation", "cleanup-candidate", "--run-id", "sensitive-invalid-value"],
            "INVALID_CANDIDATE_RUN_ID",
        ),
        (["--operation", "init", "--generation", str(uuid.uuid4())], "UNEXPECTED_GENERATION"),
    ),
)
def test_recovery_argument_errors_are_specific_and_private(tmp_path, capsys, arguments, code):
    root = tmp_path / "private"
    assert (
        cli.main(["--game", "genshin", "--uid", UID_A, "--storage-root", str(root), *arguments])
        == 1
    )
    output = capsys.readouterr().out
    assert f"ERROR_CODE={code}" in output
    assert UID_A not in output and "sensitive-invalid-value" not in output
    assert not root.exists()


def test_invalid_operation_explains_choices_without_echo(capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(["--operation", "sensitive-invalid-value"])
    assert error.value.code == 2
    output = capsys.readouterr().err
    assert "PARAMETER=--operation" in output and "cleanup-generation" in output
    assert "sensitive-invalid-value" not in output


def test_documented_cleanup_command_and_repeated_recovery(tmp_path, monkeypatch, capsys):
    root = tmp_path / "private"
    store = user_workspace.LocalSnapshotStore(
        cli.CONTRACT_ROOT,
        user_workspace.direct_configuration("genshin", UID_A, storage_root=root),
        "genshin",
        candidate_policy=candidate_policy_for("genshin"),
    )
    with store.locked():
        old = store._install(None)
        remove = user_workspace.remove_owned_tree

        def fail_cleanup(path):
            if path == old:
                raise OSError("synthetic old generation cleanup failure")
            return remove(path)

        monkeypatch.setattr(user_workspace, "remove_owned_tree", fail_cleanup)
        current = store._install(None)
        monkeypatch.undo()
        assert store.cleanup_pending
        digest = tree_digest(current)
        arguments = ["--game", "genshin", "--uid", UID_A, "--storage-root", str(root)]
    # A journal protects both possible references until recovery confirms persistence.
    assert (
        cli.main([*arguments, "--operation", "cleanup-generation", "--generation", old.name]) == 1
    )
    assert "ERROR_CODE=RECOVERY_REQUIRED" in capsys.readouterr().out
    assert old.exists() and store.current() == current
    # The documented journal-missing path permits only a complete verified non-current tree.
    store.transaction_file.unlink()
    assert cli.main([*arguments, "--operation", "inspect"]) == 0
    assert "GENERATION_STATUS=VERIFIED" in capsys.readouterr().out
    assert (
        cli.main([*arguments, "--operation", "cleanup-generation", "--generation", old.name]) == 0
    )
    assert "LOCAL_GENERATION=CLEANED" in capsys.readouterr().out
    assert not old.exists() and tree_digest(current) == digest
    assert (
        cli.main([*arguments, "--operation", "cleanup-generation", "--generation", current.name])
        == 1
    )
    assert "ERROR_CODE=CURRENT_GENERATION_PROTECTED" in capsys.readouterr().out
    for _ in range(2):
        assert cli.main([*arguments, "--operation", "recover"]) == 0
        assert store.current() == current and tree_digest(current) == digest
        assert not store.transaction_file.exists()
