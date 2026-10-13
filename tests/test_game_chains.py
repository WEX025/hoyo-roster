"""Real Providers, guards, collectors, normalization, lifecycle, candidate and storage.

Only browser/network surfaces and their time pacing are synthetic.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlparse, urlencode
import pytest
from hoyo_roster.cli import main, run, build_parser, CONTRACT_ROOT
from hoyo_roster.storage.common import tree_digest, load_metadata
from hoyo_roster.user_workspace import LocalSnapshotStore, direct_configuration
from tests.test_login_flow import UID_A, Page, Response, binding_payload


@dataclass(eq=False)
class Request:
    url: str
    method: str = "GET"
    post_data_json: object = None
    redirected_from: object = None

    @property
    def post_data(self):
        return json.dumps(self.post_data_json) if self.post_data_json is not None else None

    @property
    def headers(self):
        return {"x-rpc-device_id": "synthetic-device", "x-rpc-device_fp": "synthetic-fp"}

    async def all_headers(self):
        return {
            "x-rpc-device_id": "synthetic-device",
            "x-rpc-device_fp": "synthetic-fp",
            "cookie": "cookie_token_v2=synthetic-session",
        }


class Route:
    def __init__(self, request):
        self.request = request
        self.allowed = False

    async def continue_(self):
        self.allowed = True

    async def abort(self, code):
        self.allowed = False


class Network:
    def __init__(self, game, owned=True):
        self.game = game
        self.owned = owned
        self.binding_count = 0
        self.paths = []
        self.login_expired = True
        self.uid = UID_A
        self.level = 20
        self.drift = False
        self.lists = 0

    async def get(self, url, **kwargs):
        return await self.send(url, "GET", **kwargs)

    async def post(self, url, **kwargs):
        return await self.send(url, "POST", **kwargs)

    async def send(self, url, method, **kwargs):
        assert kwargs["max_redirects"] == 0
        path = urlparse(url).path
        self.paths.append((method, path))
        if "/binding/" in path:
            self.binding_count += 1
            return Response(
                binding_payload(
                    self.game,
                    self.uid,
                    retcode=10001 if self.login_expired and self.binding_count == 1 else 0,
                )
            )
        if self.game == "zzz":
            from hoyo_roster.games.zzz.endpoint_specs import (
                AVATAR_BASIC,
                AVATAR_INFO,
                CULTIVATE_AVATAR_BASIC,
                CULTIVATE_BATCH_DETAIL,
                BUDDY_INFO,
            )
            from hoyo_roster.games.zzz.normalize import (
                SKILL_TYPE_FIELDS,
                REQUIRED_PANEL_PROPERTY_IDS,
            )

            basic = {"id": 1011, "name": "Synthetic Agent", "level": self.level, "rank": 1}
            if path == AVATAR_BASIC.path:
                self.lists += 1
                basic["level"] = 21 if self.drift and self.lists % 2 == 0 else self.level
                data = {"avatar_list": [basic]}
            elif path == CULTIVATE_AVATAR_BASIC.path:
                data = {"list": [{"avatar": {"id": 1011}, "unlocked": True, "is_teaser": False}]}
            elif path == AVATAR_INFO.path:
                data = {
                    "avatar_list": [
                        {
                            "id": 1011,
                            "skills": [{"skill_type": i, "level": 1} for i in SKILL_TYPE_FIELDS],
                            "properties": [
                                {
                                    "property_id": i,
                                    "final": "5.1234567890123456789%" if i == 5 else "100",
                                }
                                for i in REQUIRED_PANEL_PROPERTY_IDS
                            ],
                            "weapon": None,
                            "equip": [],
                        }
                    ]
                }
            elif path == CULTIVATE_BATCH_DETAIL.path:
                data = {"list": [{"avatar": {"id": 1011, "promotes": 2}}]}
            elif path == BUDDY_INFO.path:
                data = {"list": []}
            else:
                raise AssertionError(path)
            return Response({"retcode": 0, "message": "synthetic", "data": data})
        from hoyo_roster.games.hsr.endpoint_specs import (
            AVATAR_LIST,
            AVATAR_DETAIL,
            AVATAR_RECOMMEND,
        )

        if path == AVATAR_LIST.path:
            self.lists += 1
            data = {
                "avatars": [
                    {
                        "item_id": 1001,
                        "item_name": "Synthetic Character",
                        "is_own": self.owned,
                        "cur_level": 21 if self.drift and self.lists % 2 == 0 else self.level,
                    }
                ]
            }
        elif path == AVATAR_DETAIL.path:
            data = {"avatar": {"item_id": 1001, "cur_level": 20}, "equipment": None}
        elif path == AVATAR_RECOMMEND.path:
            return Response({"retcode": -502008, "message": "synthetic unavailable", "data": {}})
        else:
            raise AssertionError(path)
        return Response({"retcode": 0, "message": "synthetic", "data": data})


class OfficialPage(Page):
    def __init__(self, context):
        super().__init__()
        self.context = context
        self.callbacks = {}

    def on(self, event, handler):
        self.callbacks.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        self.callbacks[event].remove(handler)

    async def wait_for_timeout(self, ms):
        await asyncio.sleep(0)

    async def goto(self, url, **kwargs):
        await super().goto(url, **kwargs)
        if self.context.game == "zzz":
            from hoyo_roster.games.zzz.browser import MIYOUSHE_ZZZ_URL

            if url == MIYOUSHE_ZZZ_URL:
                request = Request("https://api-takumi.mihoyo.com/synthetic-public-page")
                for handler in self.callbacks.get("request", ()):
                    handler(request)
                await asyncio.sleep(0)
        else:
            from hoyo_roster.games.hsr.browser import CULTIVATION_URL

            if url == CULTIVATION_URL and self.context.routes and not self.routes:
                from hoyo_roster.games.hsr.endpoint_specs import (
                    DEVICE_FINGERPRINT,
                    AVATAR_LIST_BOOTSTRAP,
                )

                body = {name: "synthetic" for name, _ in DEVICE_FINGERPRINT.body_fields}
                await self.context.natural(
                    Request(DEVICE_FINGERPRINT.url, "POST", body),
                    {"code": 200, "device_fp": "synthetic-fp", "msg": "synthetic"},
                )
                await self.context.natural(
                    Request(AVATAR_LIST_BOOTSTRAP.url + "?" + urlencode({"game": "hkrpg"})),
                    {"avatars": []},
                )


class OfficialContext:
    def __init__(self, game, owned=True):
        self.game = game
        self.request = Network(game, owned)
        self.pages = []
        self.callbacks = {}
        self.routes = []

    async def new_page(self):
        page = OfficialPage(self)
        self.pages.append(page)
        return page

    def on(self, event, handler):
        self.callbacks.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        self.callbacks[event].remove(handler)

    async def route(self, pattern, handler):
        self.routes.append(handler)

    async def unroute(self, pattern, handler):
        self.routes.remove(handler)

    async def cookies(self):
        return [{"name": "e_nap_token", "value": "synthetic-session"}]

    async def clear_cookies(self, **kwargs):
        pass

    async def natural(self, request, data):
        route = Route(request)
        for handler in self.routes:
            await handler(route)
        assert route.allowed
        response = Response({"retcode": 0, "message": "synthetic", "data": data})
        response.url = request.url
        response.request = request

        async def headers_array():
            return []

        response.headers_array = headers_array
        for handler in self.callbacks.get("response", ()):
            handler(response)
        await asyncio.sleep(0)


@pytest.fixture
def network_pacing(monkeypatch):
    from hoyo_roster.request_gate import RequestGate

    async def reserve(self, **kwargs):
        return 0

    monkeypatch.setattr(RequestGate, "reserve", reserve)


@pytest.mark.parametrize("game,owned", [("zzz", True), ("hsr", True), ("hsr", False)])
def test_actual_game_chain_twice_and_noop(
    tmp_path, monkeypatch, capsys, network_pacing, game, owned
):
    import importlib

    domain = importlib.import_module(f"hoyo_roster.games.{game}.cli")
    browser = importlib.import_module(f"hoyo_roster.games.{game}.browser")
    if game == "hsr":
        monkeypatch.setattr(browser, "_profile_has_active_browser_root", lambda profile: False)
    context = OfficialContext(game, owned)
    sessions = []

    @asynccontextmanager
    async def simulated_browser(**kwargs):
        page = await context.new_page()
        session = browser.BrowserSession(None, context, page, kwargs["headed"])
        sessions.append(session)
        if game == "zzz":
            page.on("request", lambda request: browser._capture_rpc_headers(session, request))
        try:
            yield session
        finally:
            if game == "hsr" and session.business_guard_installed:
                await browser.remove_business_guard(session)

    monkeypatch.setattr(domain, "persistent_browser", simulated_browser)
    root = tmp_path / "private"
    argv = [
        "--game",
        game,
        "--uid",
        UID_A,
        "--storage-root",
        str(root),
        "--allow-login",
        "--login-method",
        "phone",
    ]
    assert asyncio.run(run(build_parser().parse_args(argv))) == 0
    output = capsys.readouterr().out
    assert (
        "COLLECTING_1" in output
        and "COLLECTING_2" in output
        and "VALIDATING" in output
        and "SAVING" in output
    )
    assert (
        "DATA_MODIFIED=YES" in output and UID_A not in output and "synthetic-session" not in output
    )
    store = LocalSnapshotStore(
        CONTRACT_ROOT, direct_configuration(game, UID_A, storage_root=root), game
    )
    current = store.current()
    before = tree_digest(current / "data")
    profile = load_metadata(current / "data" / store.game / "profile.json")
    assert profile["sync"]["state"] == ("complete" if game == "zzz" else "partial")
    roster = load_metadata(current / "data" / store.game / "roster.json")["characters"]
    assert bool(roster) == owned
    if game == "zzz":
        assert (
            "0.051234567890123456789"
            in (current / "data" / store.game / "builds/1011.json").read_text()
        )
    if game == "hsr":
        assert sessions[0].session_initialized and sessions[0].bootstrap_page_sealed
        assert sessions[0].initialization_attempts == 1
    context.request.login_expired = False
    assert main(argv) == 0
    output = capsys.readouterr().out
    assert "UNCHANGED" in output and "NO_OP=YES" in output
    assert tree_digest(store.current() / "data") == before
    if owned:
        context.request.level = 19
        assert main(argv) == 1
        assert tree_digest(store.current() / "data") == before
        context.request.level = 20
        context.request.drift = True
        assert main(argv) == 1
        output = capsys.readouterr().out
        assert "FAIL" in output and UID_A not in output
        assert tree_digest(store.current() / "data") == before
    if game == "hsr" and owned:
        context.request.drift = False
        context.request.owned = False
        assert main(argv) == 0
        assert tree_digest(store.current() / "data") == before
        assert (
            load_metadata(store.current() / "data" / store.game / "roster.json")["characters"]
            == roster
        )
    assert not context.routes and all(not entries for entries in context.callbacks.values())
