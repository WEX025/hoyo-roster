"""Bounded official controls against synthetic browser/network objects only."""

import asyncio
from time import monotonic
from types import SimpleNamespace

import pytest
from playwright.async_api import Error as PlaywrightError

from hoyo_roster.cli import main
from hoyo_roster.errors import PhoneInputInvalid
from hoyo_roster.phone_login import PhoneLoginAction, TERMS_TEXT, read_phone
from hoyo_roster.progress import options_from_args
from hoyo_roster.progress import LoginOptions, PhoneInput, OPTIONS
from hoyo_roster.errors import LoginConsentRequired
from tests.test_login_flow import provider_for, binding_payload, coordinate
from hoyo_roster.user_workspace import empty_workspace
from hoyo_roster.sms import SMS_PATH, SmsReceiptWatch
from tests.test_login_flow import Context

NUMBER = "19900000000"


class Control:
    def __init__(self, *, text="", href=None, checked=False, count=1, fail=False):
        self.text, self.href, self.checked = text, href, checked
        self.amount, self.fail = count, fail
        self.fills, self.clicks = [], 0

    async def count(self):
        return self.amount

    async def is_visible(self):
        return True

    async def is_enabled(self):
        return True

    async def is_checked(self):
        return self.checked

    async def inner_text(self):
        return self.text

    async def get_attribute(self, name):
        return self.href

    async def fill(self, value, **kwargs):
        self.fills.append(value)

    async def click(self, **kwargs):
        self.clicks += 1
        if self.fail:
            raise PlaywrightError("synthetic uncertain dispatch")

    async def check(self, **kwargs):
        self.clicks += 1
        if self.fail:
            raise PlaywrightError("synthetic uncertain terms check")
        self.checked = True


class Agreement(Control):
    def __init__(self):
        super().__init__(text=TERMS_TEXT)
        self.checkbox = Control()
        self.links = [
            Control(href="https://user.mihoyo.com/#/agreement?hideBack=true"),
            Control(href="https://user.mihoyo.com/#/privacyAgreement?hideBack=true"),
        ]

    def get_by_role(self, role):
        return self.checkbox

    def locator(self, selector):
        return self

    async def all(self):
        return self.links


class Page:
    url = "https://user.mihoyo.com/login-platform/index.html"
    parent_frame = None

    def __init__(self):
        self.mobile, self.agreement, self.send = Control(), Agreement(), Control()

    @property
    def frames(self):
        return [self]

    def locator(self, selector):
        return (
            self.mobile if selector in {"input#mobile", "input#mobile:visible"} else self.agreement
        )

    def get_by_text(self, text, **kwargs):
        assert text == "获取验证码"
        return self.send


class Captcha:
    def __init__(self):
        self.blocked = False

    async def ready(self, page, deadline):
        if self.blocked:
            raise asyncio.CancelledError


def step(action, page, captcha=None):
    return action.step(page, captcha_wait=captcha or Captcha(), deadline=monotonic() + 10)


def test_fill_once_wait_for_user_terms_then_click_once(capsys):
    page, watch = Page(), SmsReceiptWatch(Context([]))
    action = PhoneLoginAction(NUMBER, watch)
    asyncio.run(step(action, page))
    asyncio.run(step(action, page))
    assert page.mobile.fills == [NUMBER] and page.send.clicks == 0
    assert not page.agreement.checkbox.checked
    assert page.agreement.checkbox.clicks == 0
    assert capsys.readouterr().out.count("PROGRESS=WAITING_FOR_TERMS") == 1
    page.agreement.checkbox.checked = True  # synthetic person, never product
    asyncio.run(step(action, page))
    asyncio.run(step(action, page))
    assert page.send.clicks == 1 and action._number is None
    output = capsys.readouterr().out
    assert "SMS_SUBMITTING" in output and "SMS_REQUESTING" not in output
    assert NUMBER not in output and NUMBER not in repr(action)


@pytest.mark.parametrize(
    "invalid", ("origin", "duplicate", "extra_terms", "foreign_link", "malformed_port", "cooldown")
)
@pytest.mark.parametrize("allow_terms", (False, True))
def test_unknown_controls_or_terms_handoff_without_sending(invalid, allow_terms):
    page = Page()
    page.agreement.checkbox.checked = True
    if invalid == "origin":
        page.url = "https://user.mihoyo.com.evil.invalid/login-platform/"
    if invalid == "duplicate":
        page.mobile.amount = 2
    if invalid == "extra_terms":
        page.agreement.text += "未知额外权限"
    if invalid == "foreign_link":
        page.agreement.links[0].href = "https://evil.invalid/#/agreement?hideBack=true"
    if invalid == "malformed_port":
        page.agreement.links[0].href = "https://user.mihoyo.com:bad/#/agreement?hideBack=true"
    if invalid == "cooldown":
        page.send.amount = 0
    action = PhoneLoginAction(NUMBER, SmsReceiptWatch(Context([])), allow_terms=allow_terms)
    action.readiness.grace = 0  # This test covers terminal unknown controls, not loading waits.
    asyncio.run(step(action, page))
    asyncio.run(step(action, page))
    assert action.finished and page.send.clicks == 0 and action._number is None
    assert page.agreement.checkbox.clicks == 0


def test_authorized_terms_checked_once_before_single_sms(capsys):
    page = Page()
    action = PhoneLoginAction(NUMBER, SmsReceiptWatch(Context([])), allow_terms=True)
    asyncio.run(step(action, page))
    asyncio.run(step(action, page))
    assert page.mobile.fills == [NUMBER]
    assert page.agreement.checkbox.checked and page.agreement.checkbox.clicks == 1
    assert page.send.clicks == 1
    output = capsys.readouterr().out
    assert output.index("TERMS_ACCEPTED") < output.index("SMS_SUBMITTING")
    assert NUMBER not in output


def test_uncertain_terms_check_hands_off_without_sms_or_retry(capsys):
    page = Page()
    page.agreement.checkbox.fail = True
    action = PhoneLoginAction(NUMBER, SmsReceiptWatch(Context([])), allow_terms=True)
    asyncio.run(step(action, page))
    asyncio.run(step(action, page))
    assert page.agreement.checkbox.clicks == 1 and page.send.clicks == 0
    output = capsys.readouterr().out
    assert "TERMS_AUTOMATION_MANUAL" in output and "TERMS_ACCEPTED" not in output


@pytest.mark.parametrize("already_checked", (False, True))
def test_qr_terms_action_never_toggles_or_sends(already_checked):
    from hoyo_roster.terms_login import TermsLoginAction

    page = Page()
    page.agreement.checkbox.checked = already_checked
    action = TermsLoginAction()
    asyncio.run(step(action, page))
    asyncio.run(step(action, page))
    assert page.agreement.checkbox.checked
    assert page.agreement.checkbox.clicks == (0 if already_checked else 1)
    assert page.mobile.fills == [] and page.send.clicks == 0


def test_qr_terms_action_rejects_unknown_agreements():
    from hoyo_roster.terms_login import TermsLoginAction

    page = Page()
    page.agreement.text += "unknown permission"
    action = TermsLoginAction()
    action.readiness.grace = 0
    asyncio.run(step(action, page))
    assert action.failed and page.agreement.checkbox.clicks == 0


@pytest.mark.parametrize(
    "arguments",
    (
        ["--allow-terms"],
        ["--allow-terms", "--allow-login", "--login-method", "phone", "--headless"],
        ["--allow-terms", "--allow-login", "--login-method", "cancel"],
        ["--allow-terms", "--allow-login", "--login-method", "qr", "--operation", "inspect"],
    ),
)
def test_terms_options_require_explicit_login_permission(arguments, capsys):
    assert main(arguments) == 1
    assert "ERROR_CODE=INVALID_TERMS_LOGIN_OPTIONS" in capsys.readouterr().out


def test_manual_request_preempts_automation():
    page, watch = Page(), SmsReceiptWatch(Context([]))
    watch._request(SimpleNamespace(url="https://passport-api.mihoyo.com" + SMS_PATH, method="POST"))
    action = PhoneLoginAction(NUMBER, watch)
    asyncio.run(step(action, page))
    assert page.mobile.fills == [] and page.send.clicks == 0


def test_relative_official_terms_links_are_checked_against_sdk_origin():
    page = Page()
    page.agreement.checkbox.checked = True
    page.agreement.links[0].href = "/#/agreement?hideBack=true"
    page.agreement.links[1].href = "//#/privacyAgreement?hideBack=true"  # untrusted host syntax
    action = PhoneLoginAction(NUMBER, SmsReceiptWatch(Context([])))
    asyncio.run(step(action, page))
    assert page.send.clicks == 0
    page.agreement.links[1].href = "/#/privacyAgreement?hideBack=true"
    action = PhoneLoginAction(NUMBER, SmsReceiptWatch(Context([])))
    asyncio.run(step(action, page))
    assert page.send.clicks == 1


def test_uncertain_click_never_repeats_or_claims_request(capsys):
    page = Page()
    page.agreement.checkbox.checked, page.send.fail = True, True
    action = PhoneLoginAction(NUMBER, SmsReceiptWatch(Context([])))
    asyncio.run(step(action, page))
    asyncio.run(step(action, page))
    action.close()
    output = capsys.readouterr().out
    assert page.send.clicks == 1
    assert "SMS_ACTION_UNCERTAIN" in output and "SMS_REQUEST_NOT_OBSERVED" in output
    assert "PROGRESS=SMS_REQUESTING" not in output and "SMS_ACCEPTED" not in output


def test_captcha_prevents_phone_fill_and_send():
    page, captcha = Page(), Captcha()
    captcha.blocked = True
    action = PhoneLoginAction(NUMBER, SmsReceiptWatch(Context([])))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(step(action, page, captcha))
    action.close()
    assert page.mobile.fills == [] and page.send.clicks == 0 and action._number is None


@pytest.mark.parametrize("value", ("", "not-a-number", "19900000000 extra", "+8619900000000"))
def test_invalid_phone_is_not_reflected(value, monkeypatch, capsys):
    async def read(*args, **kwargs):
        return value

    monkeypatch.setattr("hoyo_roster.phone_login.read_terminal", read)
    with pytest.raises(PhoneInputInvalid) as raised:
        asyncio.run(read_phone())
    assert value not in str(raised.value) if value else True
    assert "19900000000" not in capsys.readouterr().out


def test_once_only_input_shared_with_discovered_role():
    args = SimpleNamespace(
        phone_stdin=True,
        allow_sms=True,
        login_method="phone",
        non_interactive=True,
        login_timeout=900,
        allow_login=True,
    )
    initial = options_from_args(args)
    initial.phone_input.used = True
    selected = SimpleNamespace(**vars(args))
    assert options_from_args(selected).phone_input is initial.phone_input
    assert options_from_args(selected).phone_input.used


@pytest.mark.parametrize("authenticated", (False, True))
def test_no_phone_read_without_consent_or_when_session_valid(
    authenticated, tmp_path, source, monkeypatch
):
    root = tmp_path / "workspace"
    empty_workspace(source, root)
    provider, context, page, gate = provider_for(
        "genshin", root, [binding_payload("genshin", retcode=0 if authenticated else 10001)]
    )

    async def forbidden():
        pytest.fail("must not read temporary phone input")

    monkeypatch.setattr("hoyo_roster.phone_login.read_phone", forbidden)
    options = LoginOptions("phone", True, allow_login=False, phone_input=PhoneInput())
    token = OPTIONS.set(options)
    try:
        if authenticated:
            assert asyncio.run(coordinate(root, provider, context, page, gate)).success
        else:
            with pytest.raises(LoginConsentRequired):
                asyncio.run(coordinate(root, provider, context, page, gate))
        assert not options.phone_input.used
    finally:
        OPTIONS.reset(token)


@pytest.mark.parametrize(
    "arguments",
    (
        ["--phone-stdin"],
        ["--allow-sms"],
        ["--phone-stdin", "--allow-sms", "--login-method", "qr"],
        ["--phone-stdin", "--allow-sms", "--login-method", "phone", "--headless"],
    ),
)
def test_invalid_phone_options_stop_before_storage_or_browser(arguments, capsys):
    assert main(arguments) == 1
    assert "ERROR_CODE=INVALID_PHONE_LOGIN_OPTIONS" in capsys.readouterr().out


@pytest.mark.parametrize(
    "text",
    (
        "我已阅读并同意《米哈游用户协议》《米哈游隐私政策》。",
        "已阅读并同意 《米哈游用户协议》 《米哈游隐私政策》 ，未注册的手机号验证通过将自动注册。",
        "已阅读并同意《米哈游用户协议》《米哈游隐私政策》",
    ),
)
def test_known_agreements_accept_cosmetic_wording_and_optional_presentation_flags(text):
    page = Page()
    page.agreement.text = text
    page.agreement.links[0].href = "https://user.mihoyo.com/#/agreement"
    page.agreement.links[1].href = "https://user.mihoyo.com/#/privacyAgreement?hideBack=false"
    action = PhoneLoginAction(NUMBER, SmsReceiptWatch(Context([])), allow_terms=True)
    asyncio.run(step(action, page))
    assert page.agreement.checkbox.checked and page.mobile.fills == [NUMBER]
    assert page.send.clicks == 1 and not action.manual


def test_missing_terms_waits_for_mount_without_reasking_or_handoff(capsys):
    page = Page()
    page.agreement.amount = 0
    action = PhoneLoginAction(NUMBER, SmsReceiptWatch(Context([])), allow_terms=True)
    asyncio.run(step(action, page))
    assert not action.finished and page.mobile.fills == []
    page.agreement.amount = 1
    asyncio.run(step(action, page))
    assert page.send.clicks == 1 and page.mobile.fills == [NUMBER] and not action.manual
    output = capsys.readouterr().out
    assert "CONTROL_REASON=TERMS_CONTAINER_NOT_READY" in output
    assert "PHONE_AUTOMATION_MANUAL" not in output


def test_another_sdk_frame_without_a_phone_field_does_not_block():
    class WithExtraFrame(Page):
        @property
        def frames(self):
            return [self, extra]

    extra = Page()
    extra.mobile.amount = 0
    page = WithExtraFrame()
    action = PhoneLoginAction(NUMBER, SmsReceiptWatch(Context([])), allow_terms=True)
    asyncio.run(step(action, page))
    assert page.mobile.fills == [NUMBER] and page.send.clicks == 1
    assert extra.mobile.fills == [] and extra.send.clicks == 0


def test_unknown_terms_eventually_handoff_with_fixed_diagnostic(monkeypatch, capsys):
    import hoyo_roster.login_controls as controls

    now = [100.0]
    monkeypatch.setattr(controls, "monotonic", lambda: now[0])
    page = Page()
    page.agreement.text += "并预约游戏消息通知"
    action = PhoneLoginAction(NUMBER, SmsReceiptWatch(Context([])), allow_terms=True)
    asyncio.run(step(action, page))
    assert not action.finished and page.agreement.checkbox.clicks == 0
    now[0] += 16
    asyncio.run(step(action, page))
    assert action.manual and page.mobile.fills == [] and page.send.clicks == 0
    output = capsys.readouterr().out
    assert output.count("CONTROL_REASON=TERMS_EXTRA_TEXT") == 1
    assert NUMBER not in output
