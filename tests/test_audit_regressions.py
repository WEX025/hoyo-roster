"""Audit minimum repros retained as regressions; synthetic services and identities only."""

import asyncio
from types import SimpleNamespace

import pytest

from hoyo_roster.storage.common import tree_digest
from hoyo_roster import cli, login_ui, request_gate
from hoyo_roster.config import Settings
from hoyo_roster.errors import (
    LoginTimeout,
    RateLimited,
    RequestScheduleInvalid,
    RiskControl,
    SmsReceiptUnknown,
    SmsRequestRejected,
)
from hoyo_roster.games.zzz.endpoints import EndpointClient
from hoyo_roster.games.zzz.errors import ApiResponseError
from hoyo_roster.games.zzz.transport import BrowserTransport
from hoyo_roster.progress import OPTIONS, LoginOptions, PhoneInput
from hoyo_roster.request_gate import RequestGate
from hoyo_roster.sms import SMS_PATH
from hoyo_roster.storage.common import (
    atomic_write_metadata,
    load_metadata,
    secure_private_directory,
)
from hoyo_roster.user_workspace import (
    empty_workspace,
)
from tests.test_login_flow import Context, UID_A, binding_payload, coordinate, provider_for


@pytest.mark.parametrize("game", ("genshin", "zzz"))
@pytest.mark.parametrize(
    "mode,error",
    [
        ("duplicate", SmsRequestRejected),
        ("failed", SmsReceiptUnknown),
        ("pending", SmsReceiptUnknown),
        ("malformed", SmsReceiptUnknown),
        ("risk", RiskControl),
        ("limit", RateLimited),
        ("accepted", None),
    ],
)
def test_sms_state_cannot_be_overridden_by_simultaneous_binding_pass(
    tmp_path, game, mode, error, capsys
):
    async def run():
        root = tmp_path / "contracts"
        empty_workspace(cli.CONTRACT_ROOT, root)
        provider, context, page, gate = provider_for(
            game,
            root,
            [
                binding_payload(game, retcode=10001),
                binding_payload(game, retcode=10001),
                binding_payload(game),
            ],
        )
        get = context.request.get
        observed = []

        async def network(url, **kwargs):
            if context.request.count == 2:
                req = SimpleNamespace(
                    url="https://passport-api.mihoyo.com" + SMS_PATH, method="POST"
                )
                for cb in list(context.callbacks["request"]):
                    cb(req)
                watch = context.callbacks["request"][0].__self__
                observed.append(watch)
                if mode == "duplicate":
                    for cb in list(context.callbacks["request"]):
                        cb(SimpleNamespace(url=req.url, method="POST"))
                elif mode == "failed":
                    for cb in list(context.callbacks["requestfailed"]):
                        cb(req)
                elif mode != "pending":

                    async def json():
                        await asyncio.sleep(0.001)  # Receipt finishes after Binding returns.
                        if mode == "risk":
                            return {"retcode": 1034}
                        if mode == "malformed":
                            return {"retcode": 0}
                        return {"retcode": 0, "data": {"sent_new": True, "countdown": 60}}

                    response = SimpleNamespace(
                        request=req, status=429 if mode == "limit" else 200, json=json
                    )
                    for cb in list(context.callbacks["response"]):
                        cb(response)
            return await get(url, **kwargs)

        context.request.get = network
        token = OPTIONS.set(LoginOptions("phone", True, timeout=0.1, allow_login=True))
        try:
            if error:
                with pytest.raises(error):
                    await coordinate(root, provider, context, page, gate)
            else:
                assert (await coordinate(root, provider, context, page, gate)).success
                assert observed[0].state == "SMS_ACCEPTED"
                assert observed[0].task.done() and not observed[0].task.cancelled()
        finally:
            OPTIONS.reset(token)
        assert all(not callbacks for callbacks in context.callbacks.values())

    asyncio.run(run())
    output = capsys.readouterr().out
    assert UID_A not in output
    if error:
        assert "PROGRESS=LOGIN_CONFIRMED" not in output
        assert "PROGRESS=VERIFYING_IDENTITY" not in output


def test_shared_discovery_never_moves_legacy_profile(tmp_path, monkeypatch):
    from tests.test_account_workflow import SharedContext, install_context, argv

    target = secure_private_directory(tmp_path / "auth/users/legacy/browser-profile")
    marker = target / "synthetic-existing.txt"
    marker.write_text("unchanged")
    before = tree_digest(target)
    install_context(monkeypatch, SharedContext(existing=True))
    assert cli.main(argv(tmp_path, "--operation", "discover")) == 0
    assert tree_digest(target) == before and marker.read_text() == "unchanged"
    assert not (tmp_path / "results").exists()


@pytest.mark.parametrize("operation", ("sync", "login", "discover"))
@pytest.mark.parametrize("game", ("genshin", "zzz"))
def test_zero_role_flow_reports_not_collected_without_cache(
    tmp_path, monkeypatch, capsys, operation, game
):
    from tests.test_account_workflow import SharedContext, install_context, argv
    from hoyo_roster.games.genshin.login_provider import GenshinLoginProvider
    from hoyo_roster.games.zzz.login_provider import ZzzLoginProvider

    async def no_roles(self, context):
        return ()

    monkeypatch.setattr(GenshinLoginProvider, "owned_roles", no_roles)
    monkeypatch.setattr(ZzzLoginProvider, "owned_roles", no_roles)
    install_context(monkeypatch, SharedContext(existing=True))
    assert cli.main(argv(tmp_path, "--game", game, "--operation", operation)) == 1
    output = capsys.readouterr().out
    assert "GAME_STATUS=NO_ROLES" in output and "COLLECTION_STATUS=NOT_COLLECTED" in output
    assert not (tmp_path / "results").exists()


def test_future_request_schedule_rejects_without_wait_or_mutation(tmp_path, monkeypatch):
    root = secure_private_directory(tmp_path / "gate")
    gate = RequestGate(Settings(external_root=root))
    state = {"next_allowed_at": 1000000}
    atomic_write_metadata(gate.path, state)
    monkeypatch.setattr(request_gate.time, "time", lambda: 100)

    async def forbidden(*args):
        pytest.fail("Corrupt future reservation must not sleep or send")

    monkeypatch.setattr(request_gate.asyncio, "sleep", forbidden)
    with pytest.raises(RequestScheduleInvalid):
        asyncio.run(gate.reserve(requested_min_interval=2))
    assert load_metadata(gate.path, private=True) == state


def test_normal_request_reservation_is_preserved_on_cancel(tmp_path, monkeypatch):
    root = secure_private_directory(tmp_path / "gate")
    gate = RequestGate(Settings(external_root=root))
    atomic_write_metadata(gate.path, {"next_allowed_at": 102})
    monkeypatch.setattr(request_gate.time, "time", lambda: 100)
    waits = []

    async def cancelled(seconds):
        waits.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(request_gate.asyncio, "sleep", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(gate.reserve(requested_min_interval=2))
    assert waits == [2]
    assert load_metadata(gate.path, private=True) == {"next_allowed_at": 104}


@pytest.mark.parametrize("stage", ("initialization", "binding"))
def test_initial_session_budget_includes_bootstrap_and_probe(tmp_path, stage):
    async def run():
        root = tmp_path / "contracts"
        empty_workspace(cli.CONTRACT_ROOT, root)
        provider, context, page, gate = provider_for("genshin", root, [binding_payload("genshin")])
        cancelled = []

        async def hung(*args, **kwargs):
            try:
                await asyncio.Future()
            finally:
                cancelled.append(True)

        if stage == "initialization":
            gate.reserve = hung
        else:
            context.request.get = hung
        token = OPTIONS.set(LoginOptions(timeout=0.01))
        try:
            with pytest.raises(LoginTimeout):
                await coordinate(root, provider, context, page, gate)
        finally:
            OPTIONS.reset(token)
        assert cancelled == [True] and not page.urls

    asyncio.run(run())


def test_zzz_boolean_retcode_is_not_success():
    with pytest.raises(ApiResponseError):
        asyncio.run(
            EndpointClient(
                BrowserTransport(
                    Context([binding_payload("zzz", retcode=False)]), request_interval=2
                )
            ).get_binding()
        )


def test_late_control_is_not_clicked_after_manual_handoff(monkeypatch, capsys):
    from hoyo_roster.games.genshin.login_provider import LOGIN_UI

    clock = [0.0]
    clicks = []

    class Control:
        async def count(self):
            return 1 if clock[0] >= 16 else 0

        async def is_visible(self):
            return True

        async def click(self, **kwargs):
            clicks.append(clock[0])

    class Page:
        frames = []
        url = "https://act.mihoyo.com/ys/event/calculator/index.html"

        def is_closed(self):
            return False

        def locator(self, selector):
            return Control()

    async def ready(page, deadline):
        pass

    async def sleep(seconds):
        clock[0] += 1

    async def authenticated():
        return clock[0] >= 18

    monkeypatch.setattr(login_ui, "monotonic", lambda: clock[0])
    monkeypatch.setattr(login_ui.asyncio, "sleep", sleep)
    assert (
        asyncio.run(
            login_ui.confirm_method_entry(
                Page(),
                "phone",
                deadline=20,
                captcha_wait=SimpleNamespace(game="genshin", target=LOGIN_UI, ready=ready),
                authenticated=authenticated,
            )
        )
        is True
    )
    assert clicks == [] and "LOGIN_PANEL_MANUAL" in capsys.readouterr().out


def test_manual_panel_handoff_also_disables_pending_phone_action(tmp_path, monkeypatch, capsys):
    from hoyo_roster import login, phone_login

    async def manual(*args, **kwargs):
        return True

    async def phone():
        return "19900000000"

    monkeypatch.setattr(login, "confirm_method_entry", manual)
    monkeypatch.setattr(phone_login, "read_phone", phone)
    root = tmp_path / "contracts"
    empty_workspace(cli.CONTRACT_ROOT, root)
    provider, context, page, gate = provider_for(
        "genshin",
        root,
        [
            binding_payload("genshin", retcode=10001),
            binding_payload("genshin"),
        ],
    )
    token = OPTIONS.set(
        LoginOptions("phone", True, timeout=1, allow_login=True, phone_input=PhoneInput())
    )
    try:
        assert asyncio.run(coordinate(root, provider, context, page, gate)).success
    finally:
        OPTIONS.reset(token)
    output = capsys.readouterr().out
    assert "PHONE_AUTOMATION_MANUAL" in output and "PHONE_ENTERED" not in output
    assert "SMS_SUBMITTING" not in output


@pytest.mark.parametrize("operation", ("sync", "login", "discover"))
def test_legacy_discovery_resume_is_explicitly_unsupported(tmp_path, capsys, operation):
    assert (
        cli.main(
            [
                "--game",
                "genshin",
                "--operation",
                operation,
                "--resume-discovery",
                "00000000-0000-0000-0000-000000000000",
                "--auth-root",
                str(tmp_path / "auth"),
            ]
        )
        == 1
    )
    assert "DISCOVERY_RESUME_UNSUPPORTED" in capsys.readouterr().out
    assert not (tmp_path / "auth").exists()


def test_legacy_discovery_directory_is_preserved(tmp_path, capsys):
    directory = secure_private_directory(tmp_path / "auth/onboarding/synthetic")
    (directory / "synthetic-state").write_text("unchanged")
    before = tree_digest(directory)
    assert (
        cli.main(
            [
                "--game",
                "genshin",
                "--resume-discovery",
                "synthetic",
                "--auth-root",
                str(tmp_path / "auth"),
            ]
        )
        == 1
    )
    assert tree_digest(directory) == before


def test_auth_failure_closes_same_account_context(tmp_path, monkeypatch, capsys):
    from tests.test_account_workflow import SharedContext, install_context, argv
    from hoyo_roster.games.genshin.login_provider import GenshinLoginProvider

    async def fail(self, context):
        raise LoginTimeout("synthetic timeout")

    context = SharedContext()
    install_context(monkeypatch, context)
    monkeypatch.setattr(GenshinLoginProvider, "owned_roles", fail)
    assert cli.main(argv(tmp_path)) == 1
    assert context.pages and all(page.closed for page in context.pages)
    assert "BROWSER_CLOSE=CLOSED" in capsys.readouterr().out
    assert not (tmp_path / "results").exists()
