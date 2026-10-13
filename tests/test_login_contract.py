"""Public static SDK contract preflight; never contact authentication endpoints."""

import asyncio
from types import SimpleNamespace
import pytest
from hoyo_roster.games.hsr.browser import guard_login_request
from hoyo_roster.games.hsr.endpoint_specs import BUSINESS_MUTATION_PATHS

PATHS = (
    "/account/ma-cn-verifier/verifier/createLoginCaptcha",
    "/account/ma-cn-passport/web/loginByMobileCaptcha",
    "/account/ma-cn-passport/web/createQRLogin",
    "/account/ma-cn-passport/web/queryQRLoginStatus",
)


class Route:
    def __init__(self, url, method="POST"):
        self.request = SimpleNamespace(url=url, method=method, post_data=None)
        self.allowed = False

    async def continue_(self):
        self.allowed = True

    async def abort(self, code):
        assert code == "blockedbyclient"


@pytest.mark.parametrize("path", PATHS)
def test_exact_official_login_posts_allowed(path):
    route = Route("https://passport-api.mihoyo.com" + path)
    asyncio.run(guard_login_request(route))
    assert route.allowed


@pytest.mark.parametrize(
    "url,method",
    [
        ("https://passport-api.mihoyo.com/account/loginByMobileCaptcha", "POST"),
        ("https://passport-api.mihoyo.com/account/ma-cn-passport/web/changePassword", "POST"),
        ("https://passport-api.mihoyo.com.evil.example" + PATHS[0], "POST"),
        ("https://passport-api.mihoyo.com:444" + PATHS[0], "POST"),
        ("https://private@passport-api.mihoyo.com" + PATHS[0], "POST"),
        ("http://passport-api.mihoyo.com" + PATHS[0], "POST"),
        ("https://passport-api.mihoyo.com" + PATHS[0] + "/extra", "POST"),
        ("https://passport-api.mihoyo.com" + PATHS[0], "PUT"),
        ("https://act-api-takumi.mihoyo.com/event/rpgcultivate/avatar/list", "GET"),
        *[("https://act-api-takumi.mihoyo.com" + path, "POST") for path in BUSINESS_MUTATION_PATHS],
    ],
)
def test_unrelated_or_business_requests_stay_blocked(url, method):
    route = Route(url, method)
    asyncio.run(guard_login_request(route))
    assert not route.allowed


@pytest.mark.parametrize("path", PATHS)
def test_auth_paths_not_admitted_by_business_guard(path):
    from hoyo_roster.games.hsr.browser import BrowserSession, guard_browser_request

    session = BrowserSession(None, None, None, True)
    route = Route("https://passport-api.mihoyo.com" + path)
    asyncio.run(guard_browser_request(session, route))
    assert not route.allowed and session.blocked_unregistered_post_count == 1


@pytest.mark.parametrize(
    "data,size",
    [
        ({"list": [{"id": 1001}], "total": 2}, 100),
        ({"list": [{"id": 1001}], "total": 1, "has_more": True}, 100),
        ({"list": [{"id": 1001}, {"id": 1001}], "total": 2}, 100),
        ({"list": [{"id": 1001}, {"id": 1002}], "total": 2}, 1),
    ],
)
def test_genshin_partial_or_unknown_pagination_refused(data, size):
    from hoyo_roster.games.genshin.normalize import validate_complete_list
    from hoyo_roster.games.genshin.errors import SyncError

    with pytest.raises(SyncError):
        validate_complete_list(data, request_size=size)
