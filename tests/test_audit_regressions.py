"""Audit minimum repros retained as regressions; synthetic services and identities only."""

import asyncio
from types import SimpleNamespace

import pytest

from hoyo_roster import cli, discovery, login_ui, request_gate
from hoyo_roster.config import Settings
from hoyo_roster.errors import (
    LoginTimeout,
    RateLimited,
    RequestScheduleInvalid,
    RiskControl,
    SmsReceiptUnknown,
    SmsRequestRejected,
)
from hoyo_roster.games.genshin.candidate import candidate_policy
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
    LocalSnapshotStore,
    WorkspaceError,
    direct_configuration,
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


def test_discovery_existing_target_retains_new_profile_and_stops_before_exact(
    tmp_path, monkeypatch, capsys
):
    auth, results = tmp_path / "auth", tmp_path / "results"
    config = direct_configuration("genshin", UID_A, storage_root=results, auth_root=auth)
    store = LocalSnapshotStore(
        cli.CONTRACT_ROOT, config, "genshin", candidate_policy=candidate_policy()
    )
    target = secure_private_directory(store.browser_profile)
    marker = target / "synthetic-existing.txt"
    marker.write_text("unchanged")
    created = []

    async def fake(game, args, scope, source):
        profile = secure_private_directory(scope / "browser-profile")
        (profile / "synthetic-new-login.txt").write_text("synthetic")
        created.append(profile)
        return profile, discovery.binding_roles(game, binding_payload(game)["data"])

    async def forbidden(*args):
        pytest.fail("Conflict must stop before starting another identity login")

    monkeypatch.setattr(discovery, "discover_game", fake)
    monkeypatch.setattr(discovery, "run_selected_roles", forbidden)
    assert (
        cli.main(
            [
                "--game",
                "genshin",
                "--operation",
                "discover",
                "--auth-root",
                str(auth),
                "--storage-root",
                str(results),
            ]
        )
        == 1
    )
    assert marker.read_text() == "unchanged" and created[0].exists()
    assert not (auth / "onboarding/selected-genshin.json").exists()
    output = capsys.readouterr().out
    assert "DISCOVERY_PROFILE_CONFLICT" in output and "DISCOVERY_SCOPE=RETAINED" in output
    assert "DISCOVERY_STATUS=PASS" not in output and UID_A not in output


@pytest.mark.parametrize("operation", ("sync", "login", "discover"))
@pytest.mark.parametrize("game", ("genshin", "zzz"))
def test_zero_role_flow_reports_not_collected_without_cache(
    tmp_path, monkeypatch, capsys, operation, game
):
    async def fake(game, args, scope, source):
        return secure_private_directory(scope / "browser-profile"), discovery.binding_roles(
            game, {"list": []}
        )

    monkeypatch.setattr(discovery, "discover_game", fake)
    auth = tmp_path / "auth"
    assert cli.main(
        [
            "--game",
            game,
            "--operation",
            operation,
            "--auth-root",
            str(auth),
            "--storage-root",
            str(tmp_path / "results"),
        ]
    ) == (0 if operation == "discover" else 1)
    output = capsys.readouterr().out
    assert "DISCOVERY_STATUS=NO_ROLES" in output and "COLLECTION_STATUS=NOT_COLLECTED" in output
    assert "DISCOVERY_STATUS=PASS" not in output
    assert not (auth / "onboarding" / f"selected-{game}.json").exists()


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
                captcha_wait=SimpleNamespace(game="genshin", ready=ready),
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


@pytest.mark.parametrize(
    "damage", ("open", "foreign-game", "copied", "missing", "busy", "missing-engine")
)
def test_discovery_resume_requires_owned_closed_scope(tmp_path, damage):
    import uuid

    parent = secure_private_directory(tmp_path / "onboarding")
    scope = secure_private_directory(parent / str(uuid.uuid4()))
    profile = secure_private_directory(scope / "browser-profile")
    secure_private_directory(profile / "chrome")
    atomic_write_metadata(profile / "browser-choice.json", {"version": 1, "channel": "chrome"})
    discovery.mark_scope(scope, "genshin", "CLOSED")
    if damage == "open":
        discovery.mark_scope(scope, "genshin", "OPEN")
    elif damage == "foreign-game":
        discovery.mark_scope(scope, "zzz", "CLOSED")
    elif damage == "copied":
        state = load_metadata(scope / "discovery-state.json", private=True)
        state["scope"] = "0" * 64
        atomic_write_metadata(scope / "discovery-state.json", state)
    elif damage == "missing":
        (scope / "discovery-state.json").unlink()
    elif damage == "missing-engine":
        (profile / "chrome").rmdir()
    else:
        (profile / "SingletonLock").touch()
    with pytest.raises(WorkspaceError):
        discovery.resume_scope(parent, "genshin", scope.name)


def test_explicit_discovery_resume_reuses_only_selected_closed_profile(tmp_path, monkeypatch):
    import uuid

    auth = tmp_path / "auth"
    parent = discovery.discovery_root(auth, cli.CONTRACT_ROOT)
    scope = secure_private_directory(parent / str(uuid.uuid4()))
    profile = secure_private_directory(scope / "browser-profile")
    secure_private_directory(profile / "chrome")
    atomic_write_metadata(profile / "browser-choice.json", {"version": 1, "channel": "chrome"})
    discovery.mark_scope(scope, "genshin", "CLOSED")
    assert discovery.resume_scope(parent, "genshin", scope.name) == scope
    visited = []

    async def fake(game, args, selected, source):
        visited.append(selected)
        return selected / "browser-profile", discovery.binding_roles(
            game, binding_payload(game)["data"]
        )

    monkeypatch.setattr(discovery, "discover_game", fake)
    assert (
        cli.main(
            [
                "--game",
                "genshin",
                "--operation",
                "discover",
                "--resume-discovery",
                scope.name,
                "--auth-root",
                str(auth),
                "--storage-root",
                str(tmp_path / "results"),
            ]
        )
        == 0
    )
    assert visited == [scope] and not scope.exists()
    assert (parent / "selected-genshin.json").exists()


def test_failed_login_marks_discovery_closed_only_after_actual_close(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from hoyo_roster.games.genshin import browser, login_provider

    scope = secure_private_directory(tmp_path / "scope")
    discovery.mark_scope(scope, "genshin", "OPEN")
    provider, context, page, gate = provider_for(
        "genshin", tmp_path / "contracts", [binding_payload("genshin", retcode=10001)], uid=None
    )
    closed = []

    @asynccontextmanager
    async def session(**kwargs):
        try:
            yield SimpleNamespace(context=context, page=page)
        finally:
            closed.append(True)

    monkeypatch.setattr(browser, "persistent_browser", session)
    monkeypatch.setattr(discovery, "RequestGate", lambda *args: gate)
    monkeypatch.setattr(login_provider, "GenshinLoginProvider", lambda *args, **kwargs: provider)
    args = SimpleNamespace(headless=False, login_timeout=0.02)
    token = OPTIONS.set(LoginOptions("phone", True, timeout=0.02, allow_login=True))
    try:
        with pytest.raises(LoginTimeout):
            asyncio.run(discovery.discover_game("genshin", args, scope, cli.CONTRACT_ROOT))
    finally:
        OPTIONS.reset(token)
    assert closed == [True]
    assert load_metadata(scope / "discovery-state.json", private=True)["state"] == "CLOSED"
