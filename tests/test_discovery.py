"""Owned-role onboarding with synthetic network, real coordinator and native storage."""

import asyncio
import copy
import os

import pytest

from hoyo_roster import cli, discovery, workflow, platform_io as io
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

    monkeypatch.setattr(workflow, "run_task", discover)
    monkeypatch.setattr(cli, "select_configuration", forbidden_identity_selection)
    argv = [] if operation == "sync" else ["--operation", operation]
    if game is not None:
        argv += ["--game", game]
    assert cli.main(argv) == 0
    assert calls == [(game, None, operation)]


@pytest.mark.parametrize("game", ("genshin", "zzz"))
def test_discovery_uses_official_binding_after_login_without_uid(game, tmp_path, capsys):
    routine, context, page, gate = provider_for(
        game,
        tmp_path / "contracts",
        [binding_payload(game, retcode=10001), binding_payload(game)],
        uid=None,
    )
    provider = discovery.DiscoveryProvider(routine)
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


@pytest.mark.parametrize("game", ("genshin", "zzz"))
@pytest.mark.parametrize(
    "kind", ["duplicate", "bad-uid", "bad-region", "bad-level", "bad-list", "bad-item"]
)
def test_discovery_invalid_official_identity_fails_closed(game, kind, tmp_path):
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
    routine, context, _, _ = provider_for(
        game,
        tmp_path / "contracts",
        [{"retcode": 0, "message": "synthetic", "data": data}],
        uid=None,
    )
    with pytest.raises(LoginBindingFailed):
        asyncio.run(routine.owned_roles(context))


@pytest.mark.parametrize("game", ("genshin", "zzz"))
def test_discovery_all_bound_roles_and_other_games(game, tmp_path):
    data = binding_payload(game)["data"]
    data["list"].extend(binding_payload(game, UID_B)["data"]["list"])
    data["list"].extend(binding_payload("hsr")["data"]["list"])
    routine, context, _, _ = provider_for(
        game,
        tmp_path / "contracts",
        [{"retcode": 0, "message": "synthetic", "data": data}],
        uid=None,
    )
    roles = asyncio.run(routine.owned_roles(context))
    assert [role.uid for role in roles] == [UID_A, UID_B]
    routine, context, _, _ = provider_for(
        game,
        tmp_path / "empty",
        [{"retcode": 0, "message": "synthetic", "data": {"list": []}}],
        uid=None,
    )
    assert asyncio.run(routine.owned_roles(context)) == ()


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
                discovery.DiscoveryProvider(routine),
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


@pytest.mark.parametrize("role_count", (1, 2))
def test_zzz_business_requires_discovered_identity_without_collecting(role_count):
    from hoyo_roster.games.zzz.endpoints import EndpointClient
    from hoyo_roster.games.zzz.errors import ConfigurationError

    data = binding_payload("zzz")["data"]
    if role_count == 2:
        data["list"].extend(binding_payload("zzz", UID_B)["data"]["list"])

    class NoBusinessRequests:
        async def request_json(self, *args, **kwargs):
            pytest.fail("Unresolved identity must never start a business request")

    with pytest.raises(ConfigurationError):
        asyncio.run(EndpointClient(NoBusinessRequests()).collect_from_binding(data, uid=None))


def test_zzz_business_selects_only_exact_discovered_role():
    from hoyo_roster.games.zzz.endpoints import select_role
    from hoyo_roster.games.zzz.errors import ConfigurationError

    data = binding_payload("zzz")["data"]
    data["list"].extend(binding_payload("zzz", UID_B)["data"]["list"])
    assert select_role(data, UID_B).uid == UID_B
    with pytest.raises(ConfigurationError):
        select_role(data, "555555555")
    data["list"].append(copy.deepcopy(data["list"][1]))
    with pytest.raises(ConfigurationError):
        select_role(data, UID_B)


@pytest.mark.parametrize(
    "argv,code",
    [
        (["--game", "hsr"], "GAME_PAUSED"),
        (["--game", "all", "--uid", UID_A], "ARGUMENTS_INVALID"),
        (["--game", "genshin", "--resume-discovery", "bad"], "DISCOVERY_RESUME_UNSUPPORTED"),
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
