"""Synthetic codes only; no real SMS, authentication, or private paths."""

import asyncio
from time import monotonic
from types import SimpleNamespace

import pytest
from playwright.async_api import Error as PlaywrightError

from hoyo_roster.errors import OtpInputInvalid, RiskControl
from hoyo_roster.otp_login import CODE_INPUT, FORM, OtpLoginAction, read_otp
from hoyo_roster.sms import SmsReceiptWatch
from tests.test_login_flow import Context
from tests.test_phone_login import Captcha, Control, Page

SYNTHETIC_CODE = "135790"


class Field(Control):
    async def is_editable(self):
        return True

    async def fill(self, value, **kwargs):
        await super().fill(value, **kwargs)
        if self.fail:
            raise PlaywrightError("synthetic uncertain code fill")


class Form(Control):
    def __init__(self):
        super().__init__()
        self.field = Field()
        self.submit = Control(text="登录")

    def locator(self, selector):
        assert selector in {CODE_INPUT, 'button[type="submit"]'}
        return self.field if selector == CODE_INPUT else self.submit


class OtpPage(Page):
    def __init__(self):
        super().__init__()
        self.form = Form()
        self.agreement.checkbox.checked = True

    def locator(self, selector):
        return self.form if selector == FORM else super().locator(selector)


def fixture_action():
    watch = SmsReceiptWatch(Context([]))
    watch.state = "SMS_ACCEPTED"
    phone = SimpleNamespace(watch=watch, manual=False, attempted=True)
    return OtpLoginAction(phone), OtpPage()


async def step(action, page, captcha=None):
    await action.step(page, captcha_wait=captcha or Captcha(), deadline=monotonic() + 5)


async def input_code():
    return SYNTHETIC_CODE


def test_prompt_fill_submit_once_and_no_secret_output(monkeypatch, capsys):
    monkeypatch.setattr("hoyo_roster.otp_login.read_otp", input_code)
    action, page = fixture_action()

    async def run():
        try:
            for _ in range(7):
                await step(action, page)
                await asyncio.sleep(0)
            assert action.task is None and action.finished
            assert page.form.field.fills == [SYNTHETIC_CODE]
            assert page.form.submit.clicks == 1
        finally:
            await action.close()

    asyncio.run(run())
    output = capsys.readouterr().out
    assert output.count("PROGRESS=OTP_INPUT_REQUIRED") == 1
    assert (
        output.index("OTP_INPUT_REQUIRED")
        < output.index("OTP_FILLED")
        < output.index("LOGIN_SUBMITTING")
    )
    assert SYNTHETIC_CODE not in output and SYNTHETIC_CODE not in repr(action)


@pytest.mark.parametrize("state", (None, "SMS_COOLDOWN"))
def test_only_confirmed_new_sms_prompts(state, monkeypatch, capsys):
    async def forbidden():
        pytest.fail("must not ask for an old or unconfirmed SMS code")

    monkeypatch.setattr("hoyo_roster.otp_login.read_otp", forbidden)
    action, page = fixture_action()
    action.watch.state = state
    asyncio.run(step(action, page))
    assert action.task is None and page.form.field.fills == []
    assert "OTP_INPUT_REQUIRED" not in capsys.readouterr().out


@pytest.mark.parametrize("invalid", ("origin", "duplicate", "terms", "submit", "form"))
def test_unknown_form_hands_off_without_prompt_or_fill(invalid, capsys):
    action, page = fixture_action()
    if invalid == "origin":
        page.url = "https://user.mihoyo.com.evil.invalid/"
    elif invalid == "duplicate":
        page.form.field.amount = 2
    elif invalid == "terms":
        page.agreement.text += "未知权限"
    elif invalid == "submit":
        page.form.submit.text = "其他操作"
    else:
        page.form.amount = 2
    asyncio.run(step(action, page))
    assert action.finished and action.task is None
    assert page.form.field.fills == [] and page.form.submit.clicks == 0
    assert "OTP_AUTOMATION_MANUAL" in capsys.readouterr().out


@pytest.mark.parametrize("failure", ("fill", "click", "terms_changed", "origin_changed"))
def test_uncertain_action_or_changed_context_never_retried(failure, monkeypatch):
    monkeypatch.setattr("hoyo_roster.otp_login.read_otp", input_code)
    action, page = fixture_action()

    async def run():
        try:
            await step(action, page)
            await asyncio.sleep(0)
            if failure == "fill":
                page.form.field.fail = True
            elif failure == "click":
                page.form.submit.fail = True
            elif failure == "terms_changed":
                page.agreement.checkbox.checked = False
            else:
                page.url = "https://foreign.invalid/"
            for _ in range(4):
                await step(action, page)
            assert action.finished and action.task is None
            assert len(page.form.field.fills) <= 1 and page.form.submit.clicks <= 1
            if failure in {"terms_changed", "origin_changed"}:
                assert page.form.field.fills == []
        finally:
            await action.close()

    asyncio.run(run())


def test_pending_input_keeps_loop_live_and_close_cancels_reader(monkeypatch):
    started, cancelled = False, False

    async def waiting():
        nonlocal started, cancelled
        started = True
        try:
            await asyncio.Event().wait()
        finally:
            cancelled = True

    monkeypatch.setattr("hoyo_roster.otp_login.read_otp", waiting)
    action, page = fixture_action()

    async def run():
        await step(action, page)
        await asyncio.sleep(0)
        await asyncio.wait_for(step(action, page), 0.2)
        assert started and action.task is not None
        await action.close()
        assert cancelled and action.task is None

    asyncio.run(run())


def test_captcha_after_fill_pauses_submit_then_resumes(monkeypatch):
    monkeypatch.setattr("hoyo_roster.otp_login.read_otp", input_code)
    action, page = fixture_action()
    cleared = asyncio.Event()

    class Challenge:
        async def ready(self, page, deadline):
            await cleared.wait()

    async def run():
        await step(action, page)
        await asyncio.sleep(0)
        await step(action, page)
        pending = asyncio.create_task(step(action, page, Challenge()))
        await asyncio.sleep(0)
        assert page.form.field.fills == [SYNTHETIC_CODE] and page.form.submit.clicks == 0
        cleared.set()
        await pending
        assert page.form.submit.clicks == 1
        await action.close()

    asyncio.run(run())


def test_protection_after_prompt_stops_fill(monkeypatch):
    monkeypatch.setattr("hoyo_roster.otp_login.read_otp", input_code)
    action, page = fixture_action()

    async def run():
        try:
            await step(action, page)
            await asyncio.sleep(0)
            action.watch.error = RiskControl("synthetic protection")
            with pytest.raises(RiskControl):
                await step(action, page)
            assert page.form.field.fills == []
        finally:
            await action.close()

    asyncio.run(run())


@pytest.mark.parametrize("invalid", ("", "12345", "1234567", "１２３４５６", "secret"))
def test_invalid_input_never_echoed(invalid, monkeypatch, capsys):
    async def read(prompt, *, hidden):
        assert hidden
        return invalid

    monkeypatch.setattr("hoyo_roster.otp_login.read_terminal", read)
    with pytest.raises(OtpInputInvalid) as error:
        asyncio.run(read_otp())
    assert str(error.value) == "SMS code input invalid"
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("game", ("genshin", "zzz"))
def test_coordinator_runs_assisted_code_and_finishes_binding(tmp_path, monkeypatch, game):
    from hoyo_roster.cli import CONTRACT_ROOT
    from hoyo_roster.progress import OPTIONS, LoginOptions, PhoneInput
    from hoyo_roster.user_workspace import empty_workspace
    from tests.test_login_flow import Response, binding_payload, coordinate, provider_for

    async def run():
        root = tmp_path / "contracts"
        empty_workspace(CONTRACT_ROOT, root)
        provider, context, page, gate = provider_for(game, root, [])
        ui = OtpPage()
        captured = []

        async def get(url, **kwargs):
            return Response(binding_payload(game, retcode=0 if ui.form.submit.clicks else 10001))

        async def phone_step(self, page, **kwargs):
            self.attempted = self.finished = True
            self.watch.state = "SMS_ACCEPTED"

        async def controls(page):
            return ui.form.field, ui.form.submit

        async def entry(*args, **kwargs):
            return False

        async def number():
            return "19900000000"

        def action_created(phone):
            action = OtpLoginAction(phone)
            captured.append(action)
            return action

        context.request.get = get
        monkeypatch.setattr("hoyo_roster.phone_login.read_phone", number)
        monkeypatch.setattr("hoyo_roster.phone_login.PhoneLoginAction.step", phone_step)
        monkeypatch.setattr("hoyo_roster.otp_login.known_controls", controls)
        monkeypatch.setattr("hoyo_roster.otp_login.read_otp", input_code)
        monkeypatch.setattr("hoyo_roster.otp_login.OtpLoginAction", action_created)
        monkeypatch.setattr("hoyo_roster.login.confirm_method_entry", entry)
        token = OPTIONS.set(
            LoginOptions("phone", True, timeout=2, allow_login=True, phone_input=PhoneInput())
        )
        try:
            result = await coordinate(root, provider, context, page, gate)
            assert result.success
            assert ui.form.field.fills == [SYNTHETIC_CODE] and ui.form.submit.clicks == 1
            assert len(captured) == 1 and captured[0].finished and captured[0].task is None
            assert all(not handlers for handlers in context.callbacks.values())
        finally:
            OPTIONS.reset(token)

    asyncio.run(run())
