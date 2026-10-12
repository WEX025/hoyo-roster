"""Actual Providers, response validation and coordinator; only browser/network are fake."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from hoyo_roster.config import Settings
from hoyo_roster.errors import (
    AuthenticationRequired,
    LoginBindingFailed,
    LoginTimeout,
    WrongAccount,
)
from hoyo_roster.login import coordinate_login
from hoyo_roster.models import LoginProbeStatus
from hoyo_roster.storage.common import tree_digest
from hoyo_roster.user_workspace import GAMES, empty_workspace, write_json

UID_A = "900000001"
UID_B = "900000002"
BIZ = {"genshin": "hk4e_cn", "zzz": "nap_cn", "hsr": "hkrpg_cn"}
REGION = {"genshin": "cn_gf01", "zzz": "prod_gf_cn", "hsr": "prod_gf_cn"}


def binding_payload(game, uid=UID_A, *, region=None, biz=None, retcode=0):
    return {
        "retcode": retcode,
        "message": "synthetic",
        "data": {
            "list": [
                {
                    "game_biz": biz or BIZ[game],
                    "game_uid": uid,
                    "region": region or REGION[game],
                    "nickname": "Synthetic User",
                    "level": 60,
                }
            ]
        },
    }


class Response:
    status = 200
    headers = {}

    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return copy.deepcopy(self.payload)

    async def text(self):
        return json.dumps(self.payload)


class RequestAPI:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.count = 0

    async def get(self, url, **kwargs):
        assert url.startswith(
            (
                "https://passport-api.mihoyo.com/binding/",
                "https://api-takumi.mihoyo.com/binding/",
            )
        )
        assert kwargs["max_redirects"] == 0
        self.count += 1
        payload = self.payloads[0]
        if len(self.payloads) > 1:
            self.payloads.pop(0)
        return Response(payload)


class Page:
    def __init__(self, *, interrupt=False):
        self.urls = []
        self.foregrounds = 0
        self.closed = False
        self.routes = []
        self.interrupt = interrupt
        self.callbacks = {}

    def on(self, event, handler):
        self.callbacks.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        self.callbacks[event].remove(handler)

    @property
    def frames(self):
        return (self,)

    def get_by_text(self, pattern):
        return self

    @property
    def first(self):
        return self

    async def is_visible(self):
        return True

    async def goto(self, url, **kwargs):
        self.urls.append(url)
        if self.interrupt and url != "about:blank":
            self.interrupt = False
            raise asyncio.CancelledError()

    async def bring_to_front(self):
        self.foregrounds += 1

    async def route(self, pattern, handler):
        self.routes.append(handler)

    async def unroute(self, pattern, handler):
        self.routes.remove(handler)

    async def close(self):
        self.closed = True

    def is_closed(self):
        return self.closed


class Context:
    def __init__(self, payloads, *, interrupt=False):
        self.request = RequestAPI(payloads)
        self.pages = []
        self.interrupt = interrupt

    async def new_page(self):
        page = Page(interrupt=self.interrupt)
        self.pages.append(page)
        return page


class NetworkGate:
    """No real request/sleep; ephemeral Settings for HSR's genuine identity store."""

    def __init__(self, root):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.settings = Settings(external_root=root, test_mode=True)
        self.count = 0

    async def reserve(self, **kwargs):
        self.count += 1


def provider_for(game, root, payloads, *, uid=UID_A, headed=True, interrupt=False):
    context = Context(payloads, interrupt=interrupt)
    page = Page(interrupt=interrupt)
    gate = NetworkGate(root.parent / f"{game}-network-state")
    if game == "genshin":
        from hoyo_roster.games.genshin.browser import OfficialCollector
        from hoyo_roster.games.genshin.login_provider import (
            create_routine_login_provider,
        )

        collector = OfficialCollector(context, page, request_interval=2, request_gate=gate)
        provider = create_routine_login_provider(
            context=context,
            page=page,
            collector=collector,
            uid=uid,
            headed=headed,
            request_gate=gate,
            request_interval=2,
        )
    elif game == "zzz":
        from hoyo_roster.games.zzz.endpoints import EndpointClient
        from hoyo_roster.games.zzz.login_provider import (
            create_routine_login_provider,
        )
        from hoyo_roster.games.zzz.transport import BrowserTransport

        endpoints = EndpointClient(BrowserTransport(context, request_interval=2, request_gate=gate))
        provider = create_routine_login_provider(
            context=context,
            page=page,
            endpoints=endpoints,
            uid=uid,
            headed=headed,
            request_gate=gate,
            request_interval=2,
        )
    else:
        from hoyo_roster.games.hsr.browser import BrowserSession, OfficialCollector
        from hoyo_roster.games.hsr.login_provider import (
            create_routine_login_provider,
        )

        session = BrowserSession(None, context, page, headed, request_gate=gate)
        collector = OfficialCollector(context, request_interval=2, request_gate=gate)
        provider = create_routine_login_provider(collector=collector, session=session, uid=uid)
    return provider, context, page, gate


async def coordinate(
    root, provider, context, page, gate, *, headed=True, waiting=None, timeout=0.05
):
    return await coordinate_login(
        repo_root=root,
        context=context,
        page=page,
        request_gate=gate,
        providers=(provider,),
        headed=headed,
        poll_interval=0.001,
        timeout=timeout,
        waiting_callback=waiting,
    )


@pytest.mark.parametrize("game", GAMES)
@pytest.mark.parametrize("uid", (UID_A, UID_B))
def test_empty_baseline_explicit_uid_uses_real_binding_and_preserves_profile(
    tmp_path, source, game, uid
):
    root = tmp_path / "workspace"
    empty_workspace(source, root)
    before = tree_digest(root / "data")
    provider, context, page, gate = provider_for(game, root, [binding_payload(game, uid)], uid=uid)
    result = asyncio.run(coordinate(root, provider, context, page, gate))
    assert result.complete_coverage_success
    assert context.request.count == 1
    assert not page.urls
    assert tree_digest(root / "data") == before


@pytest.mark.parametrize("game", GAMES)
def test_expired_session_waits_once_then_requires_exact_uid(tmp_path, source, game):
    root = tmp_path / "workspace"
    empty_workspace(source, root)
    sequence = [
        binding_payload(game, retcode=10001),
        binding_payload(game),
    ]
    provider, context, page, gate = provider_for(game, root, sequence)
    waiting = []
    result = asyncio.run(
        coordinate(root, provider, context, page, gate, waiting=lambda: waiting.append(1))
    )
    assert result.complete_coverage_success and result.interaction_required
    assert waiting == [1] and context.request.count == 2
    login_page = context.pages[-1] if game == "hsr" else page
    assert login_page.foregrounds == 1
    if game == "hsr":
        assert login_page.closed and not login_page.routes


@pytest.mark.parametrize("game", GAMES)
@pytest.mark.parametrize("wrong", ("uid", "game", "region"))
def test_headless_wrong_binding_never_passes(tmp_path, source, game, wrong):
    root = tmp_path / "workspace"
    empty_workspace(source, root)
    payload = binding_payload(
        game,
        uid=UID_B if wrong == "uid" else UID_A,
        biz="unsupported_biz" if wrong == "game" else None,
        region="unsupported_region" if wrong == "region" else None,
    )
    provider, context, page, gate = provider_for(game, root, [payload], headed=False)
    expected_error = LoginBindingFailed if game == "genshin" and wrong == "region" else WrongAccount
    with pytest.raises(expected_error):
        asyncio.run(coordinate(root, provider, context, page, gate, headed=False))
    assert context.request.count == 1 and not page.urls


@pytest.mark.parametrize("game", GAMES)
def test_first_headless_login_requires_user_and_timeout_is_explicit(tmp_path, source, game):
    root = tmp_path / "workspace"
    empty_workspace(source, root)
    provider, context, page, gate = provider_for(game, root, [binding_payload(game, retcode=10001)])
    with pytest.raises(AuthenticationRequired):
        asyncio.run(coordinate(root, provider, context, page, gate, headed=False))
    with pytest.raises(LoginTimeout):
        asyncio.run(coordinate(root, provider, context, page, gate, timeout=0))


@pytest.mark.parametrize("game", GAMES)
def test_first_login_interrupt_never_marks_success(tmp_path, source, game):
    root = tmp_path / "workspace"
    empty_workspace(source, root)
    before = tree_digest(root / "data")
    provider, context, page, gate = provider_for(
        game,
        root,
        [binding_payload(game, retcode=10001)],
        interrupt=True,
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(coordinate(root, provider, context, page, gate))
    assert tree_digest(root / "data") == before
    if game == "hsr":
        assert all(p.closed for p in context.pages)


@pytest.mark.parametrize("region", ("cn_gf01", "cn_qd01"))
def test_genshin_bootstrap_accepts_only_official_supported_region_and_pins_it(
    tmp_path, source, region
):
    root = tmp_path / "workspace"
    empty_workspace(source, root)
    other = "cn_qd01" if region == "cn_gf01" else "cn_gf01"
    provider, context, page, gate = provider_for(
        "genshin",
        root,
        [
            binding_payload("genshin", region=region),
            binding_payload("genshin", region=other),
        ],
    )
    result = asyncio.run(coordinate(root, provider, context, page, gate))
    assert result.complete_coverage_success
    assert provider._expected_region == region
    assert asyncio.run(provider.probe(context)).status is LoginProbeStatus.WRONG_ACCOUNT


def test_genshin_existing_identity_conflict_rejected_before_network(tmp_path, source):
    from hoyo_roster.games.genshin.errors import ConfigurationError

    root = tmp_path / "workspace"
    empty_workspace(source, root)
    profile = root / "data/genshin-impact/profile.json"
    document = json.loads(profile.read_text())
    document["account"]["uid"] = UID_A
    document["account"]["server"] = "cn_gf01"
    write_json(profile, document)
    provider, context, page, gate = provider_for(
        "genshin", root, [binding_payload("genshin")], uid=UID_B
    )
    with pytest.raises(ConfigurationError):
        asyncio.run(coordinate(root, provider, context, page, gate))
    assert context.request.count == 0


def test_genshin_no_explicit_identity_fails_clearly_instead_of_assertion(tmp_path, source):
    from hoyo_roster.games.genshin.browser import BrowserSession, OfficialCollector
    from hoyo_roster.games.genshin.cli import _binding_with_login
    from hoyo_roster.games.genshin.errors import AuthenticationError

    root = tmp_path / "workspace"
    empty_workspace(source, root)
    context = Context([binding_payload("genshin")])
    page = Page()
    gate = NetworkGate(tmp_path / "network")
    collector = OfficialCollector(context, page, request_interval=2, request_gate=gate)
    session = BrowserSession(None, context, page, True)
    with pytest.raises(AuthenticationError):
        asyncio.run(
            _binding_with_login(
                collector,
                session,
                repo_root=root,
                uid=None,
                job_id=None,
                request_gate=gate,
                request_interval=2,
            )
        )
    assert context.request.count == 0
