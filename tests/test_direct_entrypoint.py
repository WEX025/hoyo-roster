from __future__ import annotations


import pytest

from tests import candidate_policy_for
from hoyo_roster.storage.common import tree_digest
from hoyo_roster.user_workspace import (
    GAMES,
    LocalSnapshotStore,
    direct_configuration,
)
from tests.fixtures import UID_A, UID_B


@pytest.mark.parametrize("game", GAMES)
@pytest.mark.parametrize("uid", (UID_A, UID_B))
def test_no_config_direct_init_is_safe_and_stable(
    tmp_path, source, local_tool, monkeypatch, game, uid
):
    root = tmp_path / "private"
    argv = [
        "--game",
        game,
        "--uid",
        uid,
        "--storage-root",
        str(root),
        "--operation",
        "init",
    ]
    assert local_tool.main(argv) == 0
    config = direct_configuration(game, uid, storage_root=root)
    store = LocalSnapshotStore(source, config, game, candidate_policy=candidate_policy_for(game))
    first = store.current()
    digest = tree_digest(first / "data")
    assert local_tool.main(argv) == 0
    assert tree_digest(store.current() / "data") == digest
    assert not (root / "users.toml").exists()
    assert uid not in str(store.root)


def test_game_uid_isolates_results_without_initializing_auth(tmp_path, source):
    stores = [
        LocalSnapshotStore(
            source,
            direct_configuration(game, uid, storage_root=tmp_path / "results"),
            game,
            candidate_policy=candidate_policy_for(game),
        )
        for game in GAMES
        for uid in (UID_A, UID_B)
    ]
    assert len({store.root for store in stores}) == 6
    for store in stores:
        with store.locked():
            current = store.prepare()
            before = tree_digest(current / "data")
            assert tree_digest(store.prepare() / "data") == before
    assert not (tmp_path / "auth").exists()


@pytest.mark.parametrize(
    "arguments,code",
    [
        (["--game", "genshin", "--operation", "init"], "UID_REQUIRED"),
        (["--uid", UID_A], "GAME_REQUIRED"),
        (["--operation", "init"], "GAME_REQUIRED"),
        (["--game", "genshin", "--uid", "0"], "INVALID_UID"),
        (["--game", "genshin", "--uid", "0123"], "INVALID_UID"),
        (["--game", "genshin", "--uid", "-1"], "INVALID_UID"),
        (["--game", "genshin", "--uid", "synthetic-private-input"], "INVALID_UID"),
        (
            ["--game", "genshin", "--uid", UID_A, "--run-id", "unexpected"],
            "UNEXPECTED_CANDIDATE_RUN_ID",
        ),
    ],
)
def test_bad_arguments_have_clear_codes_and_no_private_output(local_tool, capsys, arguments, code):
    assert local_tool.main(arguments) == 1
    output = capsys.readouterr().out
    assert f"ERROR_CODE={code}" in output
    assert UID_A not in output and "synthetic-private-input" not in output


@pytest.mark.parametrize(
    "arguments",
    [
        ["--game", "invalid-private-input", "--uid", UID_A],
        ["--game", "genshin", "--uid"],
        ["--game", "genshin", "--uid", UID_A, "--headed", "--headless"],
    ],
)
def test_parser_errors_do_not_echo_sensitive_arguments(local_tool, capsys, arguments):
    with pytest.raises(SystemExit) as error:
        local_tool.main(arguments)
    assert error.value.code == 2
    output = capsys.readouterr().err
    assert "ARGUMENTS_INVALID" in output and UID_A not in output
    assert "invalid-private-input" not in output
