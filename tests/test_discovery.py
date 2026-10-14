"""Owned-role onboarding with synthetic network, real coordinator and native storage."""

import asyncio
import copy
import importlib
import os
from types import SimpleNamespace

import pytest

from hoyo_roster import cli, discovery, platform_io as io
from hoyo_roster.errors import AuthenticationRequired, LoginBindingFailed, WrongAccount
from hoyo_roster.progress import OPTIONS, LoginOptions
from hoyo_roster.storage.common import secure_private_directory
from hoyo_roster.user_workspace import WorkspaceError
from tests.test_login_flow import UID_A, UID_B, binding_payload, provider_for, coordinate


@pytest.mark.parametrize("operation", ("sync", "login", "discover"))
@pytest.mark.parametrize("game", (None, "all", "genshin", "zzz"))
def test_public_entrypoint_discovers_before_requiring_uid(monkeypatch, operation, game):
    calls = []

    async def discover(args):
        calls.append((args.game, args.uid, args.operation))
        return 0

    def forbidden_identity_selection(args):
        pytest.fail("Default login/discovery must not require a supplied UID")

    monkeypatch.setattr(discovery, "run_discovery", discover)
    monkeypatch.setattr(cli, "select_configuration", forbidden_identity_selection)
    argv = [] if operation == "sync" else ["--operation", operation]
    if game is not None:
        argv += ["--game", game]
    assert cli.main(argv) == 0
    assert calls == [(game, None, operation)]


@pytest.mark.parametrize("game", discovery.SUPPORTED_GAMES)
def test_discovery_uses_official_binding_after_login_without_uid(game, tmp_path, capsys):
    routine, context, page, gate = provider_for(
        game,
        tmp_path / "contracts",
        [binding_payload(game, retcode=10001), binding_payload(game)],
        uid=None,
    )
    provider = discovery.DiscoveryProvider(game, routine)
    token = OPTIONS.set(LoginOptions("qr", True, 2, True))
    try:
        result = asyncio.run(coordinate(cli.CONTRACT_ROOT, provider, context, page, gate))
    finally:
        OPTIONS.reset(token)
    assert result.success
    assert [(role.game, role.uid) for role in provider.roles] == [(game, UID_A)]
    output = capsys.readouterr().out
    assert UID_A not in output and "WAITING_FOR_USER" in output
    assert UID_A not in repr(provider.roles)


@pytest.mark.parametrize("game", discovery.SUPPORTED_GAMES)
@pytest.mark.parametrize(
    "kind", ["duplicate", "bad-uid", "bad-region", "bad-level", "bad-list", "bad-item"]
)
def test_discovery_invalid_official_identity_fails_closed(game, kind):
    data = binding_payload(game)["data"]
    if kind == "duplicate":
        data["list"].append(copy.deepcopy(data["list"][0]))
    elif kind == "bad-list":
        data["list"] = {}
    elif kind == "bad-item":
        data["list"].append(None)
    else:
        field, value = {
            "bad-uid": ("game_uid", True),
            "bad-region": ("region", "os_usa"),
            "bad-level": ("level", True),
        }[kind]
        data["list"][0][field] = value
    with pytest.raises(LoginBindingFailed):
        discovery.binding_roles(game, data)


@pytest.mark.parametrize("game", discovery.SUPPORTED_GAMES)
def test_discovery_all_bound_roles_and_other_games(game):
    data = binding_payload(game)["data"]
    data["list"].extend(binding_payload(game, UID_B)["data"]["list"])
    data["list"].extend(binding_payload("hsr")["data"]["list"])
    roles = discovery.binding_roles(game, data)
    assert [role.uid for role in roles] == [UID_A, UID_B]
    assert discovery.binding_roles(game, {"list": []}) == ()


def test_discovery_headless_never_opens_new_login(tmp_path):
    routine, context, page, gate = provider_for(
        "genshin",
        tmp_path / "contracts",
        [binding_payload("genshin", retcode=10001)],
        uid=None,
        headed=False,
    )
    with pytest.raises(AuthenticationRequired):
        asyncio.run(
            coordinate(
                cli.CONTRACT_ROOT,
                discovery.DiscoveryProvider("genshin", routine),
                context,
                page,
                gate,
                headed=False,
            )
        )
    assert page.urls == []


def test_discovered_genshin_region_must_match_before_collection(tmp_path):
    from hoyo_roster.user_workspace import empty_workspace

    root = io.filesystem_path(tmp_path / "contracts")
    empty_workspace(cli.CONTRACT_ROOT, root)
    provider, context, page, gate = provider_for(
        "genshin", root, [binding_payload("genshin", region="cn_qd01")]
    )
    provider.expected_region_override = "cn_gf01"
    with pytest.raises(WrongAccount):
        asyncio.run(coordinate(root, provider, context, page, gate))
    assert context.request.count == 1 and page.urls == []


def test_new_profile_adoption_never_overwrites_existing(tmp_path):
    source = secure_private_directory(tmp_path / "onboarding" / "profile")
    destination = secure_private_directory(tmp_path / "identity" / "profile")
    marker = destination / "synthetic-existing.txt"
    marker.write_text("unchanged")
    store = SimpleNamespace(browser_profile=destination, auth_root=destination.parent)
    assert discovery.adopt_new_profile(source, store) is False
    assert marker.read_text() == "unchanged" and source.exists()


def test_new_profile_adoption_moves_only_closed_profile(tmp_path):
    source = secure_private_directory(tmp_path / "onboarding" / "profile")
    target_parent = secure_private_directory(tmp_path / "identity")
    store = SimpleNamespace(browser_profile=target_parent / "profile", auth_root=target_parent)
    (source / "SingletonLock").touch()
    with pytest.raises(WorkspaceError, match="USER_WORKSPACE_BUSY"):
        discovery.adopt_new_profile(source, store)
    (source / "SingletonLock").unlink()
    from hoyo_roster.storage.common import atomic_write_metadata, load_metadata

    atomic_write_metadata(source.parent / "request-state.json", {"next_allowed_at": 1234})
    atomic_write_metadata(target_parent / "request-state.json", {"next_allowed_at": 1200})
    assert discovery.adopt_new_profile(source, store)
    assert store.browser_profile.exists() and not source.exists()
    assert io.private(store.browser_profile)
    assert load_metadata(target_parent / "request-state.json", private=True) == {
        "next_allowed_at": 1234
    }


@pytest.mark.parametrize("game", discovery.SUPPORTED_GAMES)
@pytest.mark.parametrize("extended", (False, True))
def test_private_profile_must_stay_outside_repository_with_extended_paths(tmp_path, game, extended):
    repo = secure_private_directory(tmp_path / "repo")
    profile = secure_private_directory(repo / "profile")
    browser = importlib.import_module(f"hoyo_roster.games.{game}.browser")
    with pytest.raises(browser.ConfigurationError):
        browser.resolve_browser_profile(
            tmp_path / "repo", profile if extended else tmp_path / "repo" / "profile"
        )


def test_discovery_selection_is_not_authentication(tmp_path, monkeypatch, capsys):
    from hoyo_roster.storage.common import atomic_write_metadata

    auth = tmp_path / "auth"
    parent = discovery.discovery_root(auth, cli.CONTRACT_ROOT)
    atomic_write_metadata(
        parent / "selected-genshin.json",
        {
            "authority": "hoyo-roster/owned-role-selection-v1",
            "game": "genshin",
            "roles": [{"uid": UID_A, "region": "cn_gf01"}],
        },
    )
    calls = []

    async def exact(args):
        calls.append((args.game, args.uid, args.discovered_region))
        raise WrongAccount("synthetic changed official account")

    monkeypatch.setattr(cli, "run", exact)
    args = cli.build_parser().parse_args(
        ["--game", "genshin", "--auth-root", str(auth), "--headless"]
    )
    with pytest.raises(WrongAccount):
        asyncio.run(discovery.run_discovery(args))
    assert calls == [("genshin", UID_A, "cn_gf01")]
    assert UID_A not in capsys.readouterr().out


def test_discovery_close_failure_retains_scope_and_never_saves(tmp_path, monkeypatch, capsys):
    from hoyo_roster.sync import BrowserCloseFailed

    async def fail(*args):
        raise BrowserCloseFailed("synthetic")

    monkeypatch.setattr(discovery, "discover_game", fail)
    auth = tmp_path / "auth"
    args = cli.build_parser().parse_args(["--game", "genshin", "--auth-root", str(auth)])
    with pytest.raises(BrowserCloseFailed):
        asyncio.run(discovery.run_discovery(args))
    parent = io.filesystem_path(auth / "onboarding")
    assert not (parent / "selected-genshin.json").exists()
    assert len([path for path in parent.iterdir() if path.is_dir()]) == 1
    assert "DISCOVERY_STATUS=PASS" not in capsys.readouterr().out


def test_all_games_discovery_uses_each_exact_identity(tmp_path, monkeypatch, capsys):
    visited, exact_calls = [], []

    async def fake(game, args, scope, source):
        visited.append(game)
        profile = secure_private_directory(scope / "browser-profile")
        return profile, discovery.binding_roles(game, binding_payload(game)["data"])

    async def exact(args):
        exact_calls.append((args.game, args.uid, args.discovered_region))
        return 0

    monkeypatch.setattr(discovery, "discover_game", fake)
    monkeypatch.setattr(cli, "run", exact)
    args = cli.build_parser().parse_args(
        [
            "--game",
            "all",
            "--auth-root",
            str(tmp_path / "auth"),
            "--storage-root",
            str(tmp_path / "results"),
        ]
    )
    assert asyncio.run(discovery.run_discovery(args)) == 0
    assert visited == ["genshin", "zzz"]
    assert [item[0] for item in exact_calls] == visited
    assert all(item[1] == UID_A for item in exact_calls)
    output = capsys.readouterr().out
    assert "GAME_PAUSED" in output and UID_A not in output
    # A new invocation reuses selectors, still running exact authentication per role.
    visited.clear()
    assert asyncio.run(discovery.run_discovery(args)) == 0
    assert visited == [] and len(exact_calls) == 4


@pytest.mark.parametrize(
    "argv,code",
    [
        (["--game", "hsr"], "GAME_PAUSED"),
        (["--game", "all", "--uid", UID_A], "ARGUMENTS_INVALID"),
        (["--game", "genshin", "--operation", "discover", "--uid", UID_A], "ARGUMENTS_INVALID"),
    ],
)
def test_invalid_discovery_does_not_login(argv, code, capsys):
    assert cli.main(argv) == 1
    output = capsys.readouterr().out
    assert code in output and UID_A not in output


@pytest.mark.skipif(os.name != "nt", reason="native Windows API regression")
def test_native_sid_and_long_private_paths(tmp_path):
    from hoyo_roster import windows_io

    token_sid = windows_io._sid()
    assert token_sid is not None
    parent = secure_private_directory(tmp_path / ("a" * 120) / ("b" * 120))
    from hoyo_roster.storage.common import atomic_write_metadata, load_metadata

    atomic_write_metadata(parent / "state.json", {"synthetic": True})
    assert load_metadata(parent / "state.json", private=True) == {"synthetic": True}
    assert io.private(parent)


@pytest.mark.skipif(os.name != "nt", reason="native Windows path policy")
@pytest.mark.parametrize("path", [r"\\server\share\auth", r"\\.\C:\auth", r"\\?\C:\auth"])
def test_user_device_namespace_stays_rejected(path):
    from pathlib import Path
    from hoyo_roster.user_workspace import direct_configuration

    with pytest.raises(WorkspaceError, match="UNSUPPORTED_PRIVATE_FILESYSTEM"):
        direct_configuration("genshin", UID_A, auth_root=Path(path))
