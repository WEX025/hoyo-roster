from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from tests import candidate_policy_for
from hoyo_roster.storage.common import tree_digest
from hoyo_roster.user_workspace import (
    GAMES,
    LocalSnapshotStore,
    direct_configuration,
    empty_workspace,
)
from tests.test_login_flow import (
    Context,
    NetworkGate,
    Page,
    Response,
    UID_A,
    UID_B,
    binding_payload,
)


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


def test_game_uid_isolates_data_browser_and_upgrade_preserves_it(tmp_path, source):
    stores = [
        LocalSnapshotStore(
            source,
            direct_configuration(g, uid, storage_root=tmp_path / "private"),
            g,
            candidate_policy=candidate_policy_for(g),
        )
        for g in GAMES
        for uid in (UID_A, UID_B)
    ]
    assert len({s.root for s in stores}) == 6
    assert len({s.browser_profile for s in stores}) == 6
    for store in stores:
        with store.locked():
            current = store.prepare()
            before = tree_digest(current / "data")
            store.browser_profile.mkdir(mode=0o700)
            sentinel = store.browser_profile / "synthetic-state"
            sentinel.write_text("synthetic persistent browser state")
            assert tree_digest(store.prepare() / "data") == before
            assert sentinel.read_text() == "synthetic persistent browser state"


@pytest.mark.parametrize(
    "arguments,code",
    [
        (["--game", "genshin"], "UID_REQUIRED"),
        (["--uid", UID_A], "GAME_REQUIRED"),
        ([], "GAME_REQUIRED"),
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


class BusinessPage(Page):
    """Simulated official page responses, not a replacement for _pass or login."""

    def __init__(self, uid):
        super().__init__()
        self.uid = uid
        self.callbacks = {}
        self.business_requests = 0

    def on(self, event, handler):
        self.callbacks.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        self.callbacks[event].remove(handler)

    async def wait_for_timeout(self, milliseconds):
        return None

    async def goto(self, url, **kwargs):
        from hoyo_roster.games.genshin.browser import CALCULATOR_URL
        from hoyo_roster.games.genshin.endpoint_specs import CULTIVATION_LIST

        await super().goto(url, **kwargs)
        if url != CALCULATOR_URL or not self.callbacks.get("response"):
            return
        self.business_requests += 1
        request = SimpleNamespace(
            method="POST",
            url=CULTIVATION_LIST.url,
            post_data_json={
                "uid": self.uid,
                "region": "cn_gf01",
                "lang": "zh-cn",
                "element_attr_ids": [],
                "weapon_cat_ids": [],
                "page": 1,
                "size": 100,
            },
        )
        avatar = {
            "id": 10000005,
            "name": "Synthetic Traveler",
            "avatar_level": 5,
            "level_current": 90,
            "promote_level": 6,
            "constellation_num": 0,
            "fetter_level": 1,
            "element_attr_id": 3,
            "weapon": None,
            "skill_list": [
                {"id": 100545, "group_id": 730, "level_current": 5, "max_level": 10},
                {"id": 10077, "group_id": 932, "level_current": 6, "max_level": 10},
                {"id": 10078, "group_id": 939, "level_current": 7, "max_level": 10},
            ],
        }
        response = Response({"retcode": 0, "data": {"list": [avatar], "total": 1}})
        response.url = CULTIVATION_LIST.url
        response.request = request
        for handler in tuple(self.callbacks.get("request", ())):
            handler(request)
        for handler in tuple(self.callbacks.get("response", ())):
            handler(response)


@pytest.mark.parametrize("uid", (UID_A, UID_B))
@pytest.mark.parametrize("repository_output", (False, True))
def test_direct_genshin_fresh_login_and_two_pass_sync_without_auth_stubs(
    tmp_path,
    source,
    local_tool,
    monkeypatch,
    capsys,
    uid,
    repository_output,
):
    import hoyo_roster.games.genshin.cli as domain
    from hoyo_roster.games.genshin.browser import BrowserSession, OfficialCollector

    context = Context(
        [
            binding_payload("genshin", uid, retcode=10001),
            binding_payload("genshin", uid),
        ]
    )
    page = BusinessPage(uid)

    @asynccontextmanager
    async def simulated_browser(**kwargs):
        yield BrowserSession(None, context, page, kwargs["headed"])

    async def simulated_network_pacing(self):
        return None

    monkeypatch.setattr(domain, "persistent_browser", simulated_browser)
    monkeypatch.setattr(OfficialCollector, "_throttle", simulated_network_pacing)
    root = tmp_path / "private"
    if repository_output:
        from hoyo_roster import paths

        repo = tmp_path / "clone"
        (repo / ".git").mkdir(parents=True)
        root = repo / "data"
        root.mkdir(mode=0o755)
        (root / ".gitkeep").touch()
        monkeypatch.setattr(paths, "project_repository", lambda: repo)
    args = [
        "--game",
        "genshin",
        "--uid",
        uid,
        *([] if repository_output else ["--storage-root", str(root)]),
        "--allow-login",
        "--login-method",
        "qr",
    ]
    assert local_tool.main(args) == 0
    output = capsys.readouterr().out
    assert "WAITING_FOR_USER_LOGIN=YES" in output
    assert "DATA_MODIFIED=YES" in output and uid not in output
    assert context.request.count == 3 and page.business_requests == 2
    config = direct_configuration("genshin", uid, storage_root=root)
    store = LocalSnapshotStore(
        source, config, "genshin", candidate_policy=candidate_policy_for("genshin")
    )
    current = store.current()
    before = tree_digest(current / "data")
    assert (
        json.loads((current / "data/genshin-impact/profile.json").read_text())["account"]["uid"]
        == uid
    )
    upgrade = tmp_path / "updated-code"
    empty_workspace(source, upgrade)
    validator = upgrade / "tools/validate_repository.py"
    validator.write_text(validator.read_text() + "\n# synthetic source upgrade\n")
    store.source = upgrade
    with store.locked():
        assert tree_digest(store.prepare() / "data") == before
    assert local_tool.main(args) == 0
    output = capsys.readouterr().out
    assert "NO_OP=YES" in output and "DATA_MODIFIED=NO" in output
    assert tree_digest(store.current() / "data") == before
    assert context.request.count == 5 and page.business_requests == 4
    assert all(not callbacks for callbacks in page.callbacks.values())
    context.request.payloads = [binding_payload("genshin", UID_B if uid == UID_A else UID_A)]
    assert local_tool.main([*args, "--headless"]) == 1
    output = capsys.readouterr().out
    assert "ERROR_CODE=WRONG_ACCOUNT" in output and uid not in output
    assert tree_digest(store.current() / "data") == before
    assert page.business_requests == 4


@pytest.mark.parametrize("game", ("genshin", "zzz"))
def test_direct_login_entrypoint_runs_real_game_binding_control_flow(
    tmp_path,
    local_tool,
    monkeypatch,
    capsys,
    game,
):
    import importlib

    domain = importlib.import_module(f"hoyo_roster.games.{game}.cli")
    browser = importlib.import_module(f"hoyo_roster.games.{game}.browser")
    context = Context([binding_payload(game, retcode=10001), binding_payload(game)])
    page = Page()
    gate = NetworkGate(tmp_path / "network-state")
    if game == "hsr":
        session = browser.BrowserSession(None, context, page, True, request_gate=gate)
        monkeypatch.setattr(browser, "_profile_has_active_browser_root", lambda profile: False)
    else:
        session = browser.BrowserSession(None, context, page, True)

    @asynccontextmanager
    async def simulated_browser(**kwargs):
        yield session

    async def simulated_network_pacing(self):
        return None

    monkeypatch.setattr(domain, "persistent_browser", simulated_browser)
    if game == "zzz":
        monkeypatch.setattr(domain.BrowserTransport, "_wait_for_slot", simulated_network_pacing)
    else:
        monkeypatch.setattr(domain.OfficialCollector, "_throttle", simulated_network_pacing)
    assert (
        local_tool.main(
            [
                "--game",
                game,
                "--uid",
                UID_A,
                "--storage-root",
                str(tmp_path / "private"),
                "--operation",
                "login",
                "--allow-login",
                "--login-method",
                "qr",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert output.count("WAITING_FOR_USER_LOGIN=YES") == 1
    assert "STATUS=PASS" in output and UID_A not in output
    assert context.request.count == 2


def test_cli_interruption_is_explicit_and_private(local_tool, tmp_path, monkeypatch, capsys):
    async def interrupted(args):
        raise asyncio.CancelledError()

    monkeypatch.setattr(local_tool, "run", interrupted)
    assert local_tool.main(["--game", "genshin", "--uid", UID_A]) == 130
    output = capsys.readouterr().out
    assert "ERROR_CODE=INTERRUPTED" in output and UID_A not in output
