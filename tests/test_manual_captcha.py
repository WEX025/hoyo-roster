"""Synthetic visible UI only; no browser launch, credentials, SMS or image solving."""

import asyncio
from collections import deque
from time import monotonic

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from hoyo_roster.cli import build_parser
from hoyo_roster.errors import LoginControlUnavailable, LoginTimeout, RiskControl, WrongAccount
from hoyo_roster.login_ui import ManualCaptchaWait, confirm_method_entry
from hoyo_roster.progress import OPTIONS, LoginOptions
from hoyo_roster.storage.common import tree_digest
from hoyo_roster.user_workspace import GAMES, empty_workspace
from tests.test_login_flow import (
    UID_A,
    UID_B,
    InvisibleLocator,
    Page,
    binding_payload,
    coordinate,
    provider_for,
)


class VisibleLocator(InvisibleLocator):
    def __init__(self, read):
        self.read = read

    async def is_visible(self):
        value = self.read()
        if isinstance(value, BaseException):
            raise value
        return value


def install_challenge(page, states, *, on_read=lambda: None):
    states = deque(states)

    def read():
        on_read()
        return states.popleft() if states else False

    page.locator = lambda selector: VisibleLocator(read)


@pytest.fixture
def fast_clock(monkeypatch):
    # Only time pacing is replaced. All state detection and login code stays real.
    import hoyo_roster.login as login
    import hoyo_roster.login_ui as login_ui

    clock = [0.0]
    original_sleep = asyncio.sleep

    async def sleep(seconds):
        clock[0] += seconds
        await original_sleep(0)

    monkeypatch.setattr(login_ui, "monotonic", lambda: clock[0])
    monkeypatch.setattr(login, "monotonic", lambda: clock[0])
    monkeypatch.setattr(asyncio, "sleep", sleep)
    return clock


@pytest.mark.parametrize("in_frame", (False, True))
@pytest.mark.parametrize("instruction", (False, True))
def test_visible_challenge_before_entry_waits_for_person(fast_clock, capsys, in_frame, instruction):
    page = Page()
    target = Page() if in_frame else page
    if in_frame:

        class FramedPage(Page):
            @property
            def frames(self):
                return (self, target)

        page = FramedPage()
    if instruction:
        states = deque([True, True, False])
        entry = target.get_by_text

        def text(pattern):
            if pattern.search("请依次点击图中的目标"):
                return VisibleLocator(lambda: states.popleft() if states else False)
            return entry(pattern)

        target.get_by_text = text
    else:
        install_challenge(target, [True, True, False])
    token = OPTIONS.set(LoginOptions("phone", True, timeout=1))
    try:
        asyncio.run(confirm_method_entry(page, "phone"))
    finally:
        OPTIONS.reset(token)
    output = capsys.readouterr().out
    assert output.count("PROGRESS=WAITING_FOR_MANUAL_CAPTCHA") == 1
    assert output.count("PROGRESS=CAPTCHA_CLEARED") == 1
    assert output.index("CAPTCHA_CLEARED") < output.index("LOGIN_ENTRY=OBSERVED")
    assert all(text in output for text in ("本人", "AI 不自动选择", "倒计时", "Ctrl-C"))


@pytest.mark.parametrize("text", ("短信验证码", "59秒后重新获取", "验证码已发送", "手机号"))
def test_sms_field_or_countdown_and_hidden_widget_do_not_trigger(text, capsys):
    page = Page()
    page.get_by_text = lambda pattern: VisibleLocator(lambda: bool(pattern.search(text)))
    asyncio.run(ManualCaptchaWait().ready(page, monotonic() + 1))
    assert "CAPTCHA" not in capsys.readouterr().out


@pytest.mark.parametrize("game", GAMES)
@pytest.mark.parametrize("outcome", ("pass", "wrong", "risk"))
def test_repeated_challenge_pauses_real_binding_and_keeps_exact_identity(
    tmp_path, source, game, outcome, fast_clock, capsys
):
    root = tmp_path / "workspace"
    empty_workspace(source, root)
    before = tree_digest(root / "data")
    last = binding_payload(
        game, UID_B if outcome == "wrong" else UID_A, retcode=1034 if outcome == "risk" else 0
    )
    provider, context, page, gate = provider_for(
        game,
        root,
        [binding_payload(game, retcode=10001), binding_payload(game, retcode=10001), last],
    )
    original = provider.prepare_login_or_session
    reads = []

    async def prepare(*args):
        await original(*args)
        target = getattr(provider, "_login_page", None) or page
        install_challenge(
            target,
            [False, True, True, False, True, True, False],
            on_read=lambda: reads.append(context.request.count),
        )

    # Keep the genuine prepare/finish and Providers; replace only page visibility.
    provider_type = type(provider)
    original_class = provider_type.prepare_login_or_session

    async def prepare_dispatch(self, *args):
        assert self is provider
        await prepare(*args)

    provider_type.prepare_login_or_session = prepare_dispatch
    token = OPTIONS.set(LoginOptions("phone", True, timeout=2, allow_login=True))
    try:
        if outcome == "pass":
            assert asyncio.run(coordinate(root, provider, context, page, gate)).success
        else:
            with pytest.raises(WrongAccount if outcome == "wrong" else RiskControl):
                asyncio.run(coordinate(root, provider, context, page, gate))
    finally:
        OPTIONS.reset(token)
        provider_type.prepare_login_or_session = original_class
    assert reads == [1, 1, 1, 1, 2, 2, 2]
    assert context.request.count == 3
    assert tree_digest(root / "data") == before
    output = capsys.readouterr().out
    assert output.count("PROGRESS=WAITING_FOR_MANUAL_CAPTCHA") == 2
    assert output.count("PROGRESS=CAPTCHA_CLEARED") == 2
    assert {"genshin": "原神养成计算器", "zzz": "绝区零米游社", "hsr": "星穹铁道养成工具"}[
        game
    ] in output
    assert UID_A not in output and UID_B not in output
    if game == "hsr":
        assert provider._login_page is None
        assert context.pages[-1].closed and not context.pages[-1].routes
    else:
        assert not page.closed and len(page.urls) == 1


@pytest.mark.parametrize("failure", ("timeout", "closed", "cancel", "control"))
def test_wait_stops_safely_without_cleared_or_success(failure, fast_clock, capsys):
    page = Page()
    state = ManualCaptchaWait()
    if failure == "control":
        install_challenge(page, [PlaywrightTimeoutError("synthetic timeout")] * 4)
        expected = LoginControlUnavailable
    elif failure == "cancel":
        install_challenge(page, [True, asyncio.CancelledError()])
        expected = asyncio.CancelledError
    elif failure == "closed":
        calls = [0]

        def close():
            calls[0] += 1
            page.closed = calls[0] >= 2

        install_challenge(page, [True, True], on_read=close)
        expected = LoginControlUnavailable
    else:
        install_challenge(page, [True] * 4)
        expected = LoginTimeout
    with pytest.raises(expected):
        asyncio.run(state.ready(page, 0.5))
    output = capsys.readouterr().out
    assert "CAPTCHA_CLEARED" not in output and "LOGIN_ENTRY=OBSERVED" not in output
    assert output.count("PROGRESS=CONTROL_RETRY") <= 1
    assert output.count("PROGRESS=WAITING_FOR_MANUAL_CAPTCHA") <= 1


@pytest.mark.parametrize("failure", (PlaywrightTimeoutError, PlaywrightError))
def test_transient_control_recovers_without_duplicate_challenge(failure, fast_clock, capsys):
    page = Page()
    install_challenge(page, [True, failure("errAETimeout"), True, False])
    asyncio.run(ManualCaptchaWait().ready(page, 1))
    output = capsys.readouterr().out
    assert output.count("PROGRESS=CONTROL_RETRY") == 1
    assert output.count("PROGRESS=WAITING_FOR_MANUAL_CAPTCHA") == 1
    assert output.count("PROGRESS=CAPTCHA_CLEARED") == 1


def test_cli_help_explains_manual_captcha_and_sms_boundary():
    output = build_parser().format_help()
    assert all(text in output for text in ("CAPTCHA", "本人", "AI", "倒计时", "Ctrl-C", "900"))
