"""Obsolete entrypoints cannot revive old authentication or business-file authority."""

import asyncio

import pytest

from hoyo_roster import cli
from tests.test_login_flow import UID_A, binding_payload, coordinate, provider_for


@pytest.mark.parametrize("game", ("genshin", "zzz"))
@pytest.mark.parametrize("explicit_uid", (True, False))
def test_login_identity_does_not_read_a_business_profile(tmp_path, game, explicit_uid):
    # A corrupt business Profile must neither choose nor prevent official login.
    root = tmp_path / "workspace"
    directory = (
        root
        / "data"
        / {
            "genshin": "genshin-impact",
            "zzz": "zenless-zone-zero",
        }[game]
    )
    directory.mkdir(parents=True)
    profile = directory / "profile.json"
    profile.write_text("corrupt synthetic business data")
    provider, context, page, gate = provider_for(
        game, root, [binding_payload(game)], uid=UID_A if explicit_uid else None
    )
    if explicit_uid:
        assert asyncio.run(coordinate(root, provider, context, page, gate)).success
    else:
        assert asyncio.run(provider.expected_identity(root)).digest is None
        roles = asyncio.run(provider.owned_roles(context))
        assert [(role.game, role.uid) for role in roles] == [(game, UID_A)]
    assert context.request.count == 1
    assert profile.read_text() == "corrupt synthetic business data"


@pytest.mark.parametrize("operation", ("sync", "login", "discover", "inspect", "logout"))
@pytest.mark.parametrize("extra", ([], ["--uid", UID_A], ["--phone-stdin", "--allow-sms"]))
def test_old_resume_always_stops_before_any_private_path(tmp_path, capsys, operation, extra):
    auth, results = tmp_path / "auth", tmp_path / "results"
    assert (
        cli.main(
            [
                "--game",
                "genshin",
                "--operation",
                operation,
                "--resume-discovery",
                "synthetic",
                "--auth-root",
                str(auth),
                "--storage-root",
                str(results),
                *extra,
            ]
        )
        == 1
    )
    output = capsys.readouterr().out
    assert "ERROR_CODE=DISCOVERY_RESUME_UNSUPPORTED" in output
    assert "先核验已有身份会话" not in output
    assert not auth.exists() and not results.exists()


def test_offline_result_init_does_not_validate_or_create_auth_root(tmp_path, capsys):
    results = tmp_path / "results"
    assert (
        cli.main(
            [
                "--game",
                "genshin",
                "--uid",
                UID_A,
                "--operation",
                "init",
                "--storage-root",
                str(results),
                "--auth-root",
                "relative-unused-auth",
            ]
        )
        == 0
    )
    assert "LOCAL_WORKSPACE=READY" in capsys.readouterr().out
    assert list(results.rglob("current.json"))
    assert not (tmp_path / "relative-unused-auth").exists()
