"""Synthetic restart, SDK readiness, consent and browser-control regression evidence."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from hoyo_roster.browser import (
    guard_session_initialization,
    initialize_session_page,
    navigate_login_page,
)
from hoyo_roster.errors import (
    AuthenticationRequired,
    LoginBindingFailed,
    LoginCancelled,
    LoginConsentRequired,
    LoginControlUnavailable,
    LoginNetworkError,
    LoginTimeout,
    RateLimited,
    RiskControl,
    WrongAccount,
)
from hoyo_roster.login_ui import choose_method, confirm_method_entry
from hoyo_roster.progress import OPTIONS, LoginOptions
from hoyo_roster.user_workspace import empty_workspace
from tests.test_login_flow import (
    Context,
    Page,
    UID_A,
    UID_B,
    binding_payload,
    coordinate,
    provider_for,
)


@pytest.mark.parametrize("game", ("genshin", "zzz", "hsr"))
@pytest.mark.parametrize("headed", (False, True))
def test_sdk_initialized_before_first_binding(tmp_path, source, game, headed):
    root = tmp_path / "workspace"
    empty_workspace(source, root)
    provider, context, page, gate = provider_for(game, root, [binding_payload(game)], headed=headed)
    original = context.request.get

    async def binding(*args, **kwargs):
        assert context.pages and context.pages[0].closed
        assert context.pages[0].urls and not context.pages[0].foregrounds
        return await original(*args, **kwargs)

    context.request.get = binding
    assert asyncio.run(coordinate(root, provider, context, page, gate, headed=headed)).success


@pytest.mark.parametrize("game", ("genshin", "zzz", "hsr"))
@pytest.mark.parametrize(
    "status,error",
    ((10001, AuthenticationRequired), (1034, RiskControl), (12345, LoginBindingFailed)),
)
def test_reopened_rejected_session_stays_rejected(tmp_path, source, game, status, error):
    root = tmp_path / "workspace"
    empty_workspace(source, root)
    provider, context, page, gate = provider_for(
        game, root, [binding_payload(game, retcode=status)], headed=False
    )
    with pytest.raises(error):
        asyncio.run(coordinate(root, provider, context, page, gate, headed=False))
    assert context.request.count == 1 and not page.urls


@pytest.mark.parametrize("game", ("genshin", "zzz", "hsr"))
@pytest.mark.parametrize("headed", (False, True))
def test_wrong_account_stops_without_login(tmp_path, source, game, headed):
    root = tmp_path / "workspace"
    empty_workspace(source, root)
    provider, context, page, gate = provider_for(
        game, root, [binding_payload(game, UID_B)], headed=headed
    )
    with pytest.raises(WrongAccount):
        asyncio.run(coordinate(root, provider, context, page, gate, headed=headed))
    assert context.request.count == 1 and not page.urls


@pytest.mark.parametrize(
    "failure,error",
    (
        (PlaywrightTimeoutError("synthetic"), LoginTimeout),
        (PlaywrightError("net::ERR_CONNECTION_RESET"), LoginNetworkError),
        (PlaywrightError("synthetic private control failure"), LoginControlUnavailable),
        (asyncio.CancelledError(), asyncio.CancelledError),
    ),
)
def test_initialization_failure_closes_page_before_any_binding(failure, error):
    class FailedPage(Page):
        async def goto(self, *args, **kwargs):
            raise failure

    class FailedContext(Context):
        async def new_page(self):
            page = FailedPage()
            self.pages.append(page)
            return page

    context = FailedContext([])
    with pytest.raises(error):
        asyncio.run(initialize_session_page(context, "https://www.miyoushe.com/ys/"))
    assert context.pages[0].closed and context.request.count == 0


@pytest.mark.parametrize(
    "phase,failure,closed,error",
    (
        ("navigation", PlaywrightTimeoutError("synthetic"), False, LoginTimeout),
        ("navigation", TimeoutError("synthetic"), False, LoginTimeout),
        ("navigation", PlaywrightError("net::ERR_CONNECTION_RESET"), False, LoginNetworkError),
        (
            "navigation",
            PlaywrightError("synthetic control failure"),
            False,
            LoginControlUnavailable,
        ),
        ("navigation", PlaywrightError("Target page closed"), True, LoginControlUnavailable),
        ("navigation", PlaywrightError("net::ERR_CONNECTION_RESET"), True, LoginControlUnavailable),
        ("foreground", PlaywrightTimeoutError("synthetic"), False, LoginControlUnavailable),
        ("foreground", TimeoutError("synthetic"), False, LoginControlUnavailable),
        (
            "foreground",
            PlaywrightError("net::ERR_CONNECTION_RESET"),
            False,
            LoginControlUnavailable,
        ),
        ("foreground", PlaywrightError("Target page closed"), True, LoginControlUnavailable),
        ("navigation", asyncio.CancelledError(), False, asyncio.CancelledError),
        ("foreground", asyncio.CancelledError(), False, asyncio.CancelledError),
        ("navigation", RuntimeError("unexpected failure"), False, RuntimeError),
        ("foreground", RuntimeError("unexpected failure"), False, RuntimeError),
    ),
)
def test_login_page_failure_classification(phase, failure, closed, error):
    events = []

    class FailedPage(Page):
        def is_closed(self):
            return closed

        async def goto(self, *args, **kwargs):
            events.append("navigation")
            if phase == "navigation":
                raise failure
            return SimpleNamespace(status=200)

        async def bring_to_front(self):
            events.append("foreground")
            raise failure

    with pytest.raises(error):
        asyncio.run(navigate_login_page(FailedPage(), "https://www.miyoushe.com/ys/"))
    assert events == (["navigation"] if phase == "navigation" else ["navigation", "foreground"])


@pytest.mark.parametrize("phase", ("navigation", "foreground"))
def test_login_page_phase_deadline_is_bounded(phase):
    events = []

    class HungPage(Page):
        async def goto(self, *args, **kwargs):
            assert kwargs == {"wait_until": "domcontentloaded", "timeout": 10}
            events.append("navigation")
            if phase == "navigation":
                await asyncio.Future()
            return SimpleNamespace(status=200)

        async def bring_to_front(self):
            events.append("foreground")
            await asyncio.Future()

    token = OPTIONS.set(LoginOptions(timeout=0.01))
    try:
        with pytest.raises(LoginTimeout if phase == "navigation" else LoginControlUnavailable):
            asyncio.run(navigate_login_page(HungPage(), "https://www.miyoushe.com/ys/"))
    finally:
        OPTIONS.reset(token)
    assert events == (["navigation"] if phase == "navigation" else ["navigation", "foreground"])


@pytest.mark.parametrize("status,error", ((429, RateLimited), (503, LoginNetworkError)))
def test_login_page_http_failure_precedes_foreground_control(status, error):
    class FailedPage(Page):
        async def goto(self, *args, **kwargs):
            return SimpleNamespace(status=status)

        async def bring_to_front(self):
            pytest.fail("HTTP failure must stop before foreground control")

    with pytest.raises(error):
        asyncio.run(navigate_login_page(FailedPage(), "https://www.miyoushe.com/ys/"))


@pytest.mark.parametrize("game", ("genshin", "zzz", "hsr"))
@pytest.mark.parametrize("phase", ("navigation", "foreground"))
def test_login_page_control_failure_stops_before_further_binding(tmp_path, source, game, phase):
    root = tmp_path / "workspace"
    empty_workspace(source, root)
    provider, context, page, gate = provider_for(
        game, root, [binding_payload(game, retcode=10001), binding_payload(game)]
    )
    original_new_page = context.new_page

    async def failed_goto(url, **kwargs):
        if url != "about:blank" and phase == "navigation":
            page.closed = True
            raise PlaywrightError("Target page closed")
        return SimpleNamespace(status=200)

    async def failed_foreground():
        raise PlaywrightTimeoutError("synthetic control timeout")

    page.goto = failed_goto
    page.bring_to_front = failed_foreground

    async def new_page():
        if context.pages:  # HSR opens a separate manual-login page after SDK initialization.
            context.pages.append(page)
            return page
        return await original_new_page()

    context.new_page = new_page
    token = OPTIONS.set(LoginOptions("qr", True, timeout=0.1, allow_login=True))
    try:
        with pytest.raises(LoginControlUnavailable):
            asyncio.run(coordinate(root, provider, context, page, gate))
    finally:
        OPTIONS.reset(token)
    assert context.request.count == 1
    assert context.pages[0].closed
    if game == "hsr":
        assert page.closed and not page.routes and provider._login_page is None


@pytest.mark.parametrize(
    "url,method,resource,allowed",
    (
        ("https://act.mihoyo.com/ys/event/calculator/index.html", "GET", "document", True),
        ("https://webstatic.mihoyo.com/sdk.js", "GET", "script", True),
        (
            "https://passport-api.mihoyo.com/account/ma-cn-session/web/verifyCookieToken",
            "POST",
            "fetch",
            True,
        ),
        (
            "https://passport-api.mihoyo.com/account/ma-cn-verifier/verifier/createLoginCaptcha",
            "POST",
            "fetch",
            False,
        ),
        (
            "https://passport-api.mihoyo.com/account/ma-cn-passport/web/createQRLogin",
            "POST",
            "fetch",
            False,
        ),
        ("https://act-api-takumi.mihoyo.com/event/rpgcultivate/avatar/list", "GET", "fetch", False),
        (
            "https://api-takumi.mihoyo.com/binding/api/getUserGameRolesByLtoken",
            "GET",
            "fetch",
            False,
        ),
        (
            "https://api-takumi.mihoyo.com/binding/api/getUserGameRolesByLtoken",
            "GET",
            "document",
            False,
        ),
        (
            "https://act-api-takumi.mihoyo.com/event/rpgcultivate/avatar/list",
            "GET",
            "script",
            False,
        ),
        ("https://passport-api.mihoyo.com/account/session", "GET", "image", False),
        ("https://evil.example/sdk.js", "GET", "script", False),
        ("https://mihoyo.com.evil.example/sdk.js", "GET", "script", False),
        ("http://webstatic.mihoyo.com/sdk.js", "GET", "script", False),
    ),
)
def test_initialization_guard(url, method, resource, allowed):
    class Route:
        request = SimpleNamespace(url=url, method=method, resource_type=resource)
        accepted = False

        async def continue_(self):
            self.accepted = True

        async def abort(self, code):
            self.accepted = False

    route = Route()
    asyncio.run(guard_session_initialization(route))
    assert route.accepted == allowed


def test_notice_precedes_single_interactive_consent(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    prompts = []

    def answer(prompt):
        assert "登录前说明" in capsys.readouterr().out
        prompts.append(prompt)
        return "2"

    monkeypatch.setattr("builtins.input", answer)
    token = OPTIONS.set(LoginOptions())
    try:
        assert asyncio.run(choose_method()) == "phone"
        assert asyncio.run(choose_method()) == "phone"
    finally:
        OPTIONS.reset(token)
    assert len(prompts) == 1 and "允许" in prompts[0]


def test_noninteractive_method_does_not_imply_consent(capsys):
    token = OPTIONS.set(LoginOptions("phone", True))
    try:
        with pytest.raises(LoginConsentRequired):
            asyncio.run(choose_method())
    finally:
        OPTIONS.reset(token)
    output = capsys.readouterr().out
    assert all(
        text in output for text in ("协议", "隐私", "额外权限", "短信", "Ctrl-C", "不会自动勾选")
    )


@pytest.mark.parametrize("answer", ("n", "", "unknown"))
def test_declined_preselected_method_cancels(monkeypatch, answer):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    token = OPTIONS.set(LoginOptions("qr"))
    try:
        with pytest.raises(LoginCancelled):
            asyncio.run(choose_method())
    finally:
        OPTIONS.reset(token)


@pytest.mark.parametrize("control_timeout", (False, True))
def test_slow_or_temporarily_uncontrolled_entry_can_recover(control_timeout, capsys):
    class SlowPage(Page):
        calls = 0

        async def is_visible(self):
            self.calls += 1
            if control_timeout and self.calls == 1:
                raise PlaywrightTimeoutError("synthetic errAETimeout")
            return self.calls >= 2

    token = OPTIONS.set(LoginOptions("phone", True, timeout=0.6, allow_login=True))
    try:
        asyncio.run(confirm_method_entry(SlowPage(), "phone"))
    finally:
        OPTIONS.reset(token)
    output = capsys.readouterr().out
    assert "LOGIN_ENTRY=OBSERVED" in output and "METHOD_UNAVAILABLE" not in output
    if control_timeout:
        assert "CONTROL_RETRY" in output


def test_hung_control_is_bounded_and_classified(capsys):
    class HungPage(Page):
        async def is_visible(self):
            await asyncio.Future()

    token = OPTIONS.set(LoginOptions("qr", True, timeout=0.01))
    try:
        with pytest.raises(LoginControlUnavailable):
            asyncio.run(confirm_method_entry(HungPage(), "qr"))
    finally:
        OPTIONS.reset(token)
    assert "METHOD_UNAVAILABLE" not in capsys.readouterr().out


def test_entry_wait_can_be_cancelled():
    class CancelledPage(Page):
        async def is_visible(self):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(confirm_method_entry(CancelledPage(), "phone"))


def run_mock_process(storage, operation, headed):
    """Real CLI/providers/storage; Chromium and all responses are synthetic.

    Only our synthetic status marker crosses processes. No cookies or credentials
    are created, read, exported or modified. Not a Chromium persistence test.
    """
    from hoyo_roster.cli import main
    from hoyo_roster import platform_io as io
    from hoyo_roster import browser_runtime
    from hoyo_roster.games.genshin import browser
    from hoyo_roster.request_gate import RequestGate
    from tests.test_direct_entrypoint import BusinessPage

    events = []

    class MockContext(Context):
        def __init__(self, profile):
            super().__init__([])
            self.marker = io.filesystem_path(Path(profile)) / "synthetic-state.json"
            self.saved = self.marker.exists()
            self.valid = False
            self.request = self
            self.pages = [MockPage(self)]

        async def get(self, url, **kwargs):
            events.append("binding-pass" if self.valid else "binding-auth-required")
            from tests.test_login_flow import Response

            return Response(binding_payload("genshin", retcode=0 if self.valid else 10001))

        async def new_page(self):
            page = MockPage(self)
            self.pages.append(page)
            return page

        async def close(self):
            events.append("context-close")
            if self.valid:
                self.marker.write_text('{"synthetic_verified":true}')

    class MockPage(BusinessPage):
        def __init__(self, context):
            super().__init__(UID_A)
            self.context = context

        async def goto(self, url, **kwargs):
            if self.routes:
                events.append("initialize")
                self.context.valid = self.context.saved
            else:
                self.context.valid = True  # Synthetic manual login / already bound business page.
            await super().goto(url, **kwargs)

    class Chromium:
        executable_path = __file__

        async def launch_persistent_context(self, **kwargs):
            events.append("launch-headed" if not kwargs["headless"] else "launch-headless")
            return MockContext(kwargs["user_data_dir"])

    class Runtime:
        chromium = Chromium()

        async def start(self):
            return self

        async def stop(self):
            events.append("runtime-stop")

    browser.async_playwright = Runtime
    browser_runtime.preferred_channel = lambda: "chromium"

    async def no_wait(self, **kwargs):
        return None

    RequestGate.reserve = no_wait
    browser.OfficialCollector._throttle = no_wait
    argv = [
        "--game",
        "genshin",
        "--uid",
        UID_A,
        "--storage-root",
        storage,
        "--auth-root",
        str(Path(storage).parent / "synthetic-auth"),
        "--operation",
        operation,
        "--non-interactive",
    ]
    if headed:
        argv += ["--login-method", "phone", "--allow-login"]
    else:
        argv += ["--headless"]
    code = main(argv)
    print("SYNTHETIC_EVENTS=" + json.dumps(events))
    return code


@pytest.mark.parametrize("headed", (False, True))
def test_login_then_new_process_sync_reuses_same_synthetic_profile(tmp_path, headed):
    command = "from tests.test_session_handoff import run_mock_process; import sys; sys.exit(run_mock_process(*sys.argv[1:3], sys.argv[3]=='True'))"

    def run(operation, foreground):
        return subprocess.run(
            [sys.executable, "-c", command, str(tmp_path / "private"), operation, str(foreground)],
            env={**os.environ, "PYTHONPATH": "src", "PYTHONDONTWRITEBYTECODE": "1"},
            text=True,
            capture_output=True,
            timeout=30,
        )

    login = run("login", True)
    assert login.returncode == 0, login.stdout + login.stderr
    assert "binding-auth-required" in login.stdout and "STATUS=PASS" in login.stdout
    sync = run("sync", headed)
    assert sync.returncode == 0, sync.stdout + sync.stderr
    assert "DATA_MODIFIED=YES" in sync.stdout and "binding-auth-required" not in sync.stdout
    assert "登录前说明" not in sync.stdout
    assert '"context-close", "runtime-stop"' in sync.stdout
    assert UID_A not in login.stdout + sync.stdout


@pytest.mark.parametrize("game", ("genshin", "zzz"))
@pytest.mark.parametrize("close_failure", (False, True))
@pytest.mark.parametrize("headed", (False, True))
def test_persistent_close_finishes_before_runtime_stop(
    tmp_path, monkeypatch, game, close_failure, headed
):
    import importlib
    from hoyo_roster import browser_runtime

    browser = importlib.import_module(f"hoyo_roster.games.{game}.browser")
    events = []

    class PersistentContext(Context):
        async def close(self):
            events.append("close")
            await asyncio.sleep(0)
            if close_failure:
                raise PlaywrightError("synthetic close failure")
            events.append("flushed")

    context = PersistentContext([])

    class Chromium:
        executable_path = __file__

        async def launch_persistent_context(self, **kwargs):
            from hoyo_roster import platform_io as io
            from hoyo_roster.browser import chromium_profile_path

            assert kwargs["user_data_dir"] == chromium_profile_path(
                io.filesystem_path(tmp_path / "profile" / "chromium")
            )
            assert kwargs["channel"] == "chromium"
            assert kwargs["handle_sigint"] is False
            assert kwargs["headless"] is (not headed)
            return context

    class Runtime:
        chromium = Chromium()

        async def start(self):
            return self

        async def stop(self):
            events.append("stop")

    monkeypatch.setattr(browser, "async_playwright", Runtime)
    monkeypatch.setattr(browser_runtime, "preferred_channel", lambda: "chromium")
    if game == "zzz":

        async def new_page():
            from tests.test_game_chains import OfficialContext

            return await OfficialContext("zzz").new_page()

        context.new_page = new_page

    async def use():
        async with browser.persistent_browser(
            repo_root=tmp_path / "repo", browser_profile=tmp_path / "profile", headed=headed
        ):
            events.append("use")

    if close_failure:
        with pytest.raises(PlaywrightError):
            asyncio.run(use())
        assert events == ["use", "close", "stop"]
    else:
        asyncio.run(use())
        assert events == ["use", "close", "flushed", "stop"]


@pytest.mark.parametrize("message", ("errAETimeout", "timeoutReached"))
def test_external_timeout_markers_are_recoverable(message, capsys):
    class PageBridge(Page):
        calls = 0

        async def is_visible(self):
            self.calls += 1
            if self.calls == 1:
                raise PlaywrightError(message)
            return True

    token = OPTIONS.set(LoginOptions("qr", True, timeout=0.5))
    try:
        asyncio.run(confirm_method_entry(PageBridge(), "qr"))
    finally:
        OPTIONS.reset(token)
    assert "CONTROL_RETRY" in capsys.readouterr().out


@pytest.mark.parametrize("retcode,error", ((1034, RiskControl), (12345, None)))
def test_session_refresh_protection_or_invalid_response_stops(tmp_path, source, retcode, error):
    from hoyo_roster.errors import LoginSessionInitializationFailed
    from tests.test_login_flow import Response

    root = tmp_path / "workspace"
    empty_workspace(source, root)
    provider, context, page, gate = provider_for("genshin", root, [binding_payload("genshin")])

    class RefreshPage(Page):
        async def goto(self, url, **kwargs):
            response = Response({"retcode": retcode})
            response.url = (
                "https://passport-api.mihoyo.com/account/ma-cn-session/web/verifyCookieToken"
            )
            for handler in self.callbacks.get("response", ()):
                handler(response)
            await asyncio.sleep(0)

    async def new_page():
        page = RefreshPage()
        context.pages.append(page)
        return page

    context.new_page = new_page
    with pytest.raises(error or LoginSessionInitializationFailed):
        asyncio.run(coordinate(root, provider, context, page, gate))
    assert context.request.count == 0 and context.pages[0].closed


def test_rate_limited_initialization_stops_before_binding(tmp_path, source):
    from hoyo_roster.errors import RateLimited

    root = tmp_path / "workspace"
    empty_workspace(source, root)
    provider, context, page, gate = provider_for("genshin", root, [binding_payload("genshin")])

    class RateLimitedPage(Page):
        async def goto(self, url, **kwargs):
            return SimpleNamespace(status=429)

    async def new_page():
        page = RateLimitedPage()
        context.pages.append(page)
        return page

    context.new_page = new_page
    with pytest.raises(RateLimited):
        asyncio.run(coordinate(root, provider, context, page, gate))
    assert context.request.count == 0 and context.pages[0].closed


def test_entry_can_arrive_after_old_thirty_second_cutoff(monkeypatch):
    from hoyo_roster import login_ui

    clock = {"now": 0.0}
    monkeypatch.setattr(login_ui, "monotonic", lambda: clock["now"])

    class LatePage(Page):
        calls = 0

        async def is_visible(self):
            self.calls += 1
            if self.calls == 1:
                clock["now"] = 31.0
                return False
            return True

    token = OPTIONS.set(LoginOptions("phone", True, timeout=60))
    try:
        asyncio.run(confirm_method_entry(LatePage(), "phone"))
    finally:
        OPTIONS.reset(token)


@pytest.mark.parametrize("game", ("genshin", "zzz"))
def test_cli_login_does_not_report_success_when_persistent_close_fails(
    tmp_path, monkeypatch, capsys, game
):
    from contextlib import asynccontextmanager
    import importlib
    from hoyo_roster.cli import main
    from hoyo_roster.request_gate import RequestGate

    domain = importlib.import_module(f"hoyo_roster.games.{game}.cli")
    browser = importlib.import_module(f"hoyo_roster.games.{game}.browser")
    context = Context([binding_payload(game)])
    page = Page()

    @asynccontextmanager
    async def failed_close(**kwargs):
        session = browser.BrowserSession(None, context, page, kwargs["headed"])
        yield session
        raise PlaywrightError("synthetic context close failure")

    async def no_wait(self, **kwargs):
        return None

    monkeypatch.setattr(domain, "persistent_browser", failed_close)
    monkeypatch.setattr(RequestGate, "reserve", no_wait)
    if game == "hsr":
        monkeypatch.setattr(browser, "_profile_has_active_browser_root", lambda profile: False)
    assert (
        main(
            [
                "--game",
                game,
                "--uid",
                UID_A,
                "--operation",
                "login",
                "--storage-root",
                str(tmp_path / "private"),
                "--headless",
                "--non-interactive",
            ]
        )
        == 1
    )
    output = capsys.readouterr().out
    assert "\nSTATUS=PASS" not in output and "STABLE_CODE=PASS" not in output
    assert "LOCAL_WORKSPACE=FAIL" in output and UID_A not in output
