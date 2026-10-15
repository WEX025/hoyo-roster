"""Account SSO and independent game storage using real pipelines, fake browser/network."""

import asyncio
import importlib
import shutil
from types import SimpleNamespace
from contextlib import asynccontextmanager

import pytest

from hoyo_roster import auth, cli
from hoyo_roster.errors import WrongAccount
from hoyo_roster.models import OwnedRole
from hoyo_roster.user_workspace import LocalSnapshotStore, direct_configuration
from tests import candidate_policy_for
from tests.test_direct_entrypoint import BusinessPage
from tests.test_game_chains import Network, Request
from tests.test_login_flow import Response, UID_A, UID_B, binding_payload


class SharedNetwork:
    def __init__(self, context):
        self.context = context
        self.zzz = Network("zzz")
        self.zzz.login_expired = False

    async def get(self, url, **kwargs):
        if "/binding/" in url:
            game = {"hk4e_cn": "genshin", "nap_cn": "zzz"}[kwargs["params"]["game_biz"]]
            if game == "zzz" and self.context.expire_before_zzz:
                self.context.expire_before_zzz = False
                self.context.logged_in = False
            if (
                game == "genshin"
                and self.context.expire_during_pass
                and self.context.business_requests == 1
            ):
                self.context.expire_during_pass = False
                self.context.logged_in = False
            return Response(binding_payload(game, retcode=0 if self.context.logged_in else 10001))
        return await self.zzz.get(url, **kwargs)

    async def post(self, url, **kwargs):
        return await self.zzz.post(url, **kwargs)


class SharedPage(BusinessPage):
    def __init__(self, context):
        super().__init__(UID_A)
        self.context = context

    async def goto(self, url, **kwargs):
        from hoyo_roster.games.genshin.browser import CALCULATOR_URL
        from hoyo_roster.games.zzz.browser import MIYOUSHE_ZZZ_URL

        # Simulate the person confirming QR in the actual login page, after bootstrap.
        if (
            url in {CALCULATOR_URL, MIYOUSHE_ZZZ_URL}
            and not self.routes
            and not self.context.logged_in
        ):
            self.context.logged_in = True
            self.context.logins += 1
        before = self.business_requests
        await super().goto(url, **kwargs)
        self.context.business_requests += self.business_requests - before
        if url == MIYOUSHE_ZZZ_URL:
            request = Request("https://api-takumi.mihoyo.com/synthetic-public-page")
            for handler in tuple(self.callbacks.get("request", ())):
                handler(request)


class SharedContext:
    def __init__(self, *, existing=False, between=False, during=False):
        self.logged_in = existing
        self.expire_before_zzz = between
        self.expire_during_pass = during
        self.pages = []
        self.callbacks = {}
        self.logins = 0
        self.business_requests = 0
        self.request = SharedNetwork(self)

    async def new_page(self):
        page = SharedPage(self)
        self.pages.append(page)
        return page

    def on(self, event, handler):
        self.callbacks.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        self.callbacks[event].remove(handler)

    async def cookies(self):
        return [{"name": "e_nap_token", "value": "synthetic-session"}]

    async def clear_cookies(self, **kwargs):
        pass


def install_context(monkeypatch, context, *, close_fails=False):
    opened = []

    @asynccontextmanager
    async def browser(profile, *, headed):
        opened.append(profile)
        try:
            yield context
        finally:
            for page in context.pages:
                await page.close()
            if close_fails:
                raise RuntimeError("synthetic close failure")

    async def reserve(self, **kwargs):
        return 0

    monkeypatch.setattr(auth, "account_browser", browser)
    monkeypatch.setattr(auth.RequestGate, "reserve", reserve)
    return opened


def argv(tmp_path, *extra):
    return [
        "--storage-root",
        str(tmp_path / "results"),
        "--auth-root",
        str(tmp_path / "auth"),
        "--allow-login",
        "--non-interactive",
        "--login-method",
        "qr",
        *extra,
    ]


def store_for(tmp_path, game):
    return LocalSnapshotStore(
        cli.CONTRACT_ROOT,
        direct_configuration(
            game, UID_A, storage_root=tmp_path / "results", auth_root=tmp_path / "auth"
        ),
        game,
        candidate_policy=candidate_policy_for(game),
    )


@pytest.mark.parametrize(
    "existing,between,logins", [(False, False, 1), (True, False, 0), (False, True, 2)]
)
def test_default_real_pipelines_share_one_context(
    tmp_path, monkeypatch, capsys, existing, between, logins
):
    context = SharedContext(existing=existing, between=between)
    opened = install_context(monkeypatch, context)
    assert cli.main(argv(tmp_path)) == 0
    assert len(opened) == 1 and context.logins == logins
    assert all(page.closed for page in context.pages)
    for game in ("genshin", "zzz"):
        store = store_for(tmp_path, game)
        current = store.current()
        assert current is not None
        assert {p.name for p in (current / "data").iterdir()} == {"manifest.json", store.game}
        assert {p.name for p in (current / "schemas").iterdir()} == {
            "common",
            "repository-manifest.schema.json",
            store.game,
        }
    output = capsys.readouterr().out
    assert output.index("GAME=genshin") < output.index("GAME=zzz")
    assert UID_A not in output and "synthetic-session" not in output


def test_auth_loss_in_pass_two_restarts_only_current_game(tmp_path, monkeypatch, capsys):
    context = SharedContext(existing=True, during=True)
    opened = install_context(monkeypatch, context)
    assert cli.main(argv(tmp_path)) == 0
    assert context.business_requests == 3 and context.logins == 1
    assert context.request.zzz.lists == 2 and len(opened) == 1
    assert capsys.readouterr().out.count("AUTH_RECOVERY=RESTART_CURRENT_GAME") == 1


def test_game_failure_does_not_stop_other_game(tmp_path, monkeypatch, capsys):
    context = SharedContext(existing=True)
    install_context(monkeypatch, context)
    module = importlib.import_module("hoyo_roster.games.genshin.entry")

    async def fail(*args, **kwargs):
        raise module.SyncError("synthetic source drift")

    monkeypatch.setattr(module, "run_sync", fail)
    assert cli.main(argv(tmp_path)) == 1
    assert store_for(tmp_path, "zzz").current() is not None
    output = capsys.readouterr().out
    assert "GAME=genshin\nGAME_STATUS=FAIL" in output and "GAME=zzz\nGAME_STATUS=PASS" in output


@pytest.mark.parametrize("operation", ("login", "discover"))
def test_auth_only_does_not_initialize_business_results(tmp_path, monkeypatch, operation):
    context = SharedContext()
    opened = install_context(monkeypatch, context)
    assert cli.main(argv(tmp_path, "--operation", operation)) == 0
    assert len(opened) == 1 and context.logins == 1
    assert not (tmp_path / "results").exists()


def test_committed_games_survive_account_close_failure(tmp_path, monkeypatch, capsys):
    install_context(monkeypatch, SharedContext(existing=True), close_fails=True)
    assert cli.main(argv(tmp_path)) == 1
    assert all(store_for(tmp_path, game).current() is not None for game in ("genshin", "zzz"))
    output = capsys.readouterr().out
    assert "ERROR_CODE=BROWSER_CLOSE_FAILED" in output and "COMMIT_STATE=COMMITTED" in output
    assert "TASK_STATUS=PASS" not in output


@pytest.mark.parametrize("game", ("genshin", "zzz"))
def test_unrelated_contract_damage_does_not_block_game(tmp_path, game):
    source = tmp_path / "source"
    shutil.copytree(cli.CONTRACT_ROOT, source)
    shutil.rmtree(source / "schemas/honkai-star-rail")
    (source / "data/honkai-star-rail/profile.json").write_text("broken")
    candidate = importlib.import_module(f"hoyo_roster.games.{game}.cli")
    prevalidate = (
        candidate.prevalidate_repository
        if game == "genshin"
        else candidate._prevalidate_formal_repository
    )
    prevalidate(source)
    store = LocalSnapshotStore(
        source,
        direct_configuration(
            game, UID_A, storage_root=tmp_path / "results", auth_root=tmp_path / "auth"
        ),
        game,
        candidate_policy=candidate_policy_for(game),
    )
    with store.locked():
        current = store.prepare()
        assert store.current() == current
    assert not (tmp_path / "auth").exists()


def test_account_mutex_is_independent_of_results_and_games(tmp_path):
    async def run():
        a = auth.AuthService(tmp_path / "auth", headed=False, timeout=1)
        b = auth.AuthService(tmp_path / "auth", headed=False, timeout=1)
        async with a:
            with pytest.raises(ValueError, match="USER_WORKSPACE_BUSY"):
                async with b:
                    pytest.fail("same account Profile must be exclusive")
        async with b:
            assert b.context is None

    asyncio.run(run())


def test_account_change_after_reauth_stops(tmp_path, monkeypatch):
    context = SharedContext(existing=True)
    install_context(monkeypatch, context)
    from hoyo_roster.games.genshin.login_provider import GenshinLoginProvider

    async def run():
        async with auth.AuthService(tmp_path / "auth", headed=True, timeout=2) as service:
            async with service.page() as page:
                provider = GenshinLoginProvider(context, page, service.gate, 2)
                await service.owned_roles(provider)
                service._owned["genshin"] = (provider, (OwnedRole("genshin", UID_B, "cn_gf01"),))
                with pytest.raises(WrongAccount):
                    await service.owned_roles(provider)

    asyncio.run(run())


def test_repeated_auth_loss_is_bounded_and_does_not_commit_snapshot(tmp_path, monkeypatch, capsys):
    from hoyo_roster.games.genshin.errors import AuthenticationError
    from hoyo_roster.games.genshin.browser import OfficialCollector

    # Only replace the natural request surface: every cultivation response is unauthenticated.
    async def expired(self, *, uid, region):
        raise AuthenticationError("synthetic expired cultivation response")

    context = SharedContext(existing=True)
    install_context(monkeypatch, context)
    monkeypatch.setattr(OfficialCollector, "cultivation_list", expired)
    assert cli.main(argv(tmp_path)) == 1
    current = store_for(tmp_path, "genshin").current()
    from hoyo_roster.jsonio import load_json

    assert load_json(current / "data/genshin-impact/profile.json")["sync"]["state"] == "not_synced"
    assert store_for(tmp_path, "zzz").current() is None
    output = capsys.readouterr().out
    assert output.count("AUTH_RECOVERY=RESTART_CURRENT_GAME") == 1
    assert "PROGRESS=SAVED" not in output and "TASK_STATUS=PASS" not in output


def test_no_genshin_role_continues_zzz_without_relogin(tmp_path, monkeypatch):
    from hoyo_roster.games.genshin.login_provider import GenshinLoginProvider

    async def empty(self, context):
        return ()

    monkeypatch.setattr(GenshinLoginProvider, "owned_roles", empty)
    context = SharedContext(existing=True)
    install_context(monkeypatch, context)
    assert cli.main(argv(tmp_path)) == 0
    assert store_for(tmp_path, "zzz").current() is not None
    assert context.logins == 0


def test_multiple_roles_reuse_auth_and_have_independent_results(tmp_path):
    from hoyo_roster.game_runtime import execute_roles
    from hoyo_roster.sync import OperationResult
    from hoyo_roster.games.genshin.errors import SyncError

    args = cli.build_parser().parse_args(argv(tmp_path))
    roles = (OwnedRole("genshin", UID_A, "cn_gf01"), OwnedRole("genshin", UID_B, "cn_gf01"))
    service = SimpleNamespace(headed=True, gate=object())
    visited = []

    async def collect(domain, *, snapshot_store, request_gate_override, auth_service):
        assert request_gate_override is service.gate and auth_service is service
        visited.append((domain.uid, snapshot_store.root))
        return OperationResult("genshin", "sync", no_op=True)

    result = asyncio.run(
        execute_roles(
            args,
            service,
            roles,
            game="genshin",
            candidate_policy=lambda: candidate_policy_for("genshin"),
            sync=collect,
            sync_error=SyncError,
        )
    )
    assert result.status == "PASS" and len(result.roles) == 2
    assert [uid for uid, _ in visited] == [UID_A, UID_B]
    assert len({path for _, path in visited}) == 2
    assert not (tmp_path / "auth").exists()


def test_auth_import_does_not_load_business_store_or_games():
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import hoyo_roster.auth; "
            "assert 'hoyo_roster.user_workspace' not in sys.modules; "
            "assert not any(n.startswith('hoyo_roster.games.') for n in sys.modules)",
        ],
        capture_output=True,
    )
    assert result.returncode == 0
