"""Opt-in real Chromium, fully intercepted synthetic pages; no official traffic/auth."""

import asyncio
import json
import os
from time import monotonic

import pytest
from playwright.async_api import async_playwright

from hoyo_roster.errors import LoginTimeout
from hoyo_roster.login_ui import ManualCaptchaWait, confirm_method_entry
from hoyo_roster.sms import SMS_PATH, SmsReceiptWatch
from hoyo_roster.phone_login import PhoneLoginAction

pytestmark = pytest.mark.skipif(
    os.environ.get("HOYO_NATIVE_LOGIN_SMOKE") != "1", reason="explicit isolated browser smoke only"
)

ROOT = "https://act.mihoyo.com/ys/event/calculator/index.html"
SDK = "https://user.mihoyo.com/login-platform/index.html"
SMS = "https://passport-api.mihoyo.com" + SMS_PATH
QR = "<img class=\"qr-loaded\" style=\"width:80px;height:80px\" src=\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='80' height='80'%3E%3Crect width='80' height='80'/%3E%3C/svg%3E\">"


async def isolated(run, html, *, root=ROOT, game="genshin"):
    unexpected = []
    sent = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            channel=os.environ.get("HOYO_TEST_BROWSER_CHANNEL", "chromium"), headless=True
        )
        context = await browser.new_context(service_workers="block")
        context.set_default_timeout(3000)

        async def route(route):
            url = route.request.url
            if url == root:
                opening = (
                    "opens++;const f=document.createElement('iframe');f.src='"
                    + SDK
                    + "';document.body.append(f)"
                )
                panel = (
                    '<div class="tw-fixed tw-top-0"><img class="tw-cursor-pointer tw-rounded-full" style="width:20px;height:20px" onclick="'
                    + opening
                    + '"></div>'
                    if game == "genshin"
                    else '<button onclick="' + opening + '">登录</button>'
                )
                await route.fulfill(
                    body=panel + "<script>window.opens=0</script>",
                    content_type="text/html; charset=utf-8",
                )
            elif url == SDK:
                await route.fulfill(body=html, content_type="text/html; charset=utf-8")
            elif url == SMS:
                sent.append(url)
                await route.fulfill(
                    json={"retcode": 0, "data": {"sent_new": True, "countdown": 60}},
                    headers={"Access-Control-Allow-Origin": "*"},
                )
            else:
                unexpected.append(url)
                await route.abort()

        await context.route("**/*", route)
        page = await context.new_page()
        try:
            await run(context, page, sent)
            assert unexpected == []
        finally:
            await context.close()
            await browser.close()


@pytest.mark.parametrize("game", ("genshin", "zzz"))
def test_avatar_panel_and_qr_switch_once(capsys, game):
    html = (
        '<div class="qr-login-btn--wrapper"><svg width="32" height="32" onclick="switches++;document.body.innerHTML=qr"><rect width="32" height="32"/></svg></div><script>window.switches=0;window.qr='
        + json.dumps(QR)
        + "</script>"
    )

    root = ROOT if game == "genshin" else "https://www.miyoushe.com/zzz/"

    async def run(context, page, sent):
        await page.goto(root)
        await confirm_method_entry(
            page, "qr", captcha_wait=ManualCaptchaWait(game), deadline=monotonic() + 4
        )
        assert await page.evaluate("opens") == 1
        assert await page.frames[1].evaluate("switches") == 1
        assert sent == []

    asyncio.run(isolated(run, html, root=root, game=game))
    assert "QR_READY" in capsys.readouterr().out


def test_phone_terms_and_one_manual_request(capsys):
    html = (
        """<input id="mobile"><input type="checkbox" id="terms"><button onclick="if(document.querySelector('#terms').checked && document.querySelector('#mobile').value.length===11)fetch("""
        + repr(SMS)
        + """,{method:'POST'})">发送验证码</button>"""
    )

    async def run(context, page, sent):
        watch = SmsReceiptWatch(context)
        watch.start()
        try:
            await page.goto(SDK)
            await confirm_method_entry(page, "phone", deadline=monotonic() + 4)
            assert not await page.locator("#terms").is_checked()
            assert await page.locator("#mobile").input_value() == ""
            assert sent == []  # product cannot accept terms, read values or request SMS
            # Harness impersonates the person only on an entirely synthetic page.
            await page.locator("#mobile").fill("19900000000")
            await page.get_by_text("发送验证码", exact=True).click()
            assert sent == []  # unchecked agreement blocks before network
            await page.locator("#terms").check()
            await page.get_by_text("发送验证码", exact=True).click()
            deadline = monotonic() + 4
            while watch.state is None and monotonic() < deadline:
                watch.check()
                await asyncio.sleep(0.02)
            assert sent == [SMS] and watch.state == "SMS_ACCEPTED"
        finally:
            await watch.close()

    asyncio.run(isolated(run, html))
    assert "SMS_ACCEPTED" in capsys.readouterr().out


@pytest.mark.parametrize("allow_terms", (False, True))
def test_authorized_phone_fill_terms_and_one_real_dom_click(capsys, allow_terms):
    html = (
        '<input id="mobile"><label class="agree-checkbox"><input type="checkbox">'
        '已阅读并同意<a href="https://user.mihoyo.com/#/agreement?hideBack=true">《米哈游用户协议》</a>'
        '<a href="https://user.mihoyo.com/#/privacyAgreement?hideBack=true">《米哈游隐私政策》</a>'
        "，未注册的手机号验证通过将自动注册。</label>"
        '<span onclick="clicks++;fetch(' + repr(SMS) + ",{method:'POST'})\">获取验证码</span>"
        "<script>window.clicks=0</script>"
    )

    async def run(context, page, sent):
        watch = SmsReceiptWatch(context)
        watch.start()
        action = PhoneLoginAction("19900000000", watch, allow_terms=allow_terms)
        captcha = ManualCaptchaWait("genshin")
        deadline = monotonic() + 4
        try:
            await page.goto(SDK)
            await action.step(page, captcha_wait=captcha, deadline=deadline)
            await action.step(page, captcha_wait=captcha, deadline=deadline)
            assert await page.locator("#mobile").input_value() == "19900000000"
            if not allow_terms:
                assert not await page.get_by_role("checkbox").is_checked() and sent == []
                await page.get_by_role("checkbox").check()  # synthetic person confirms terms
                await action.step(page, captcha_wait=captcha, deadline=deadline)
            assert await page.get_by_role("checkbox").is_checked()
            while watch.state is None and monotonic() < deadline:
                watch.check()
                await asyncio.sleep(0.02)
            await action.step(page, captcha_wait=captcha, deadline=deadline)
            assert sent == [SMS] and watch.state == "SMS_ACCEPTED"
            assert await page.evaluate("clicks") == 1
        finally:
            action.close()
            await watch.close()

    asyncio.run(isolated(run, html))
    output = capsys.readouterr().out
    assert "19900000000" not in output
    assert output.index("TERMS_ACCEPTED" if allow_terms else "WAITING_FOR_TERMS") < output.index(
        "SMS_SUBMITTING"
    )
    assert (
        output.index("SMS_SUBMITTING")
        < output.index("SMS_REQUESTING")
        < output.index("SMS_ACCEPTED")
    )


def test_authorized_qr_terms_can_unlock_entry_before_qr_is_ready(capsys):
    from hoyo_roster.terms_login import TermsLoginAction

    html = (
        '<label class="agree-checkbox"><input type="checkbox" onchange="checks++;'
        "document.querySelector('#qr').innerHTML=qr\">"
        '已阅读并同意<a href="https://user.mihoyo.com/#/agreement?hideBack=true">《米哈游用户协议》</a>'
        '<a href="https://user.mihoyo.com/#/privacyAgreement?hideBack=true">《米哈游隐私政策》</a>'
        '，未注册的手机号验证通过将自动注册。</label><div id="qr"></div>'
        "<script>window.checks=0;window.qr=" + json.dumps(QR) + "</script>"
    )

    async def run(context, page, sent):
        await page.goto(SDK)
        action = TermsLoginAction()
        await confirm_method_entry(page, "qr", deadline=monotonic() + 4, terms_action=action)
        assert await page.get_by_role("checkbox").is_checked()
        assert await page.evaluate("checks") == 1 and sent == []

    asyncio.run(isolated(run, html))
    output = capsys.readouterr().out
    assert output.index("TERMS_ACCEPTED") < output.index("QR_READY")


def test_captcha_pauses_probes_and_disappeared_panel_accepts_binding(capsys):
    html = '<div class="geetest_panel">SYNTHETIC manual challenge</div>'

    async def run(context, page, sent):
        await page.goto(SDK)
        probes = []

        async def binding():
            probes.append(1)
            return True

        task = asyncio.create_task(
            confirm_method_entry(page, "qr", deadline=monotonic() + 4, authenticated=binding)
        )
        await asyncio.sleep(0.25)
        assert probes == []
        # Synthetic human completion removes the UI; no QR screenshot or solving.
        await page.locator(".geetest_panel").evaluate("node=>node.remove()")
        await task
        assert probes == [1] and sent == []

    asyncio.run(isolated(run, html))
    output = capsys.readouterr().out
    assert output.index("CAPTCHA_CLEARED") < output.index("LOGIN_CONFIRMED")


def test_expired_qr_is_not_ready_and_timeout_no_refresh(capsys):
    async def run(context, page, sent):
        await page.goto(SDK)
        with pytest.raises(LoginTimeout):
            await confirm_method_entry(page, "qr", deadline=monotonic() + 0.25)
        assert sent == []

    asyncio.run(isolated(run, QR + '<div class="qr-expired">已过期</div>'))
    assert "QR_READY" not in capsys.readouterr().out


def test_scanned_is_not_login_success(capsys):
    async def run(context, page, sent):
        await page.goto(SDK)
        await confirm_method_entry(page, "qr", deadline=monotonic() + 2)
        assert sent == []

    asyncio.run(isolated(run, "<div>扫码成功</div><div>请在手机上点击【确认登录】</div>"))
    output = capsys.readouterr().out
    assert "QR_SCANNED_WAIT_CONFIRM" in output and "LOGIN_CONFIRMED" not in output


def test_entry_cancel_and_page_close_release_resources():
    async def run(context, page, sent):
        await page.goto(SDK)
        task = asyncio.create_task(confirm_method_entry(page, "phone", deadline=monotonic() + 4))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not page.is_closed() and sent == []

    asyncio.run(isolated(run, "<div>Loading synthetic page</div>"))


@pytest.mark.parametrize("mode", ("qr", "scanned", "password"))
def test_switch_to_phone_never_sends(mode):
    action = "switches++;document.body.innerHTML='<input id=mobile>'"
    html = (
        '<div class="qr-wrap">'
        + QR
        + '</div><div class="qr-login-btn--wrapper"><svg width="32" height="32" onclick="'
        + action
        + '"><rect width="32" height="32"/></svg></div>'
        if mode == "qr"
        else '<button onclick="' + action + '">短信登录</button>'
    ) + "<script>window.switches=0</script>"

    if mode == "scanned":
        html = (
            '<div class="qr-login-page">扫码成功</div><div class="qr-login-btn--wrapper"><svg width="32" height="32" onclick="'
            + action
            + '"><rect width="32" height="32"/></svg></div><script>window.switches=0</script>'
        )

    async def run(context, page, sent):
        await page.goto(SDK)
        await confirm_method_entry(page, "phone", deadline=monotonic() + 4)
        assert await page.evaluate("switches") == 1
        assert sent == []

    asyncio.run(isolated(run, html))


def test_hidden_sdk_challenge_cannot_pause_visible_window(capsys):
    async def run(context, page, sent):
        await page.goto(ROOT)
        # Browser harness creates an inactive SDK remnant; the product does no JS mutation.
        await page.evaluate(
            "url=>{const f=document.createElement('iframe');f.src=url;f.style.display='none';document.body.append(f)}",
            SDK,
        )
        for _ in range(50):
            if len(page.frames) > 1 and page.frames[1].url == SDK:
                break
            await asyncio.sleep(0.02)

        async def binding():
            return True

        await confirm_method_entry(page, "qr", deadline=monotonic() + 2, authenticated=binding)
        assert sent == []

    asyncio.run(isolated(run, '<div class="geetest_panel">SYNTHETIC inactive challenge</div>'))
    output = capsys.readouterr().out
    assert "LOGIN_CONFIRMED" in output and "WAITING_FOR_MANUAL_CAPTCHA" not in output


def test_late_sdk_control_stays_manual_after_handoff(monkeypatch):
    from hoyo_roster import login_ui

    clock = [0.0]
    monkeypatch.setattr(login_ui, "monotonic", lambda: clock[0])

    async def run(context, page, sent):
        handed_off = asyncio.Event()
        emit = login_ui.emit

        def observe(code):
            emit(code)
            if code == "LOGIN_PANEL_MANUAL":
                handed_off.set()

        monkeypatch.setattr(login_ui, "emit", observe)
        await page.goto(SDK)
        task = asyncio.create_task(confirm_method_entry(page, "phone", deadline=20))
        await asyncio.sleep(0.05)  # Establish the entry budget before advancing the clock.
        clock[0] = 16
        await asyncio.wait_for(handed_off.wait(), timeout=3)
        # Entirely synthetic harness adds a late known SDK mode switch.
        await page.evaluate("""()=>{window.switches=0;document.body.innerHTML=
            '<div class="qr-login-page">pending</div><div class="qr-login-btn--wrapper">'
            +'<svg width="32" height="32" onclick="switches++"><rect width="32" height="32"/></svg></div>'}""")
        await asyncio.sleep(0.3)
        assert await page.evaluate("switches") == 0 and sent == []
        await page.evaluate("()=>document.body.insertAdjacentHTML('beforeend','<input id=mobile>')")
        assert await asyncio.wait_for(task, timeout=3) is True

    asyncio.run(isolated(run, "<div>Loading synthetic SDK</div>"))
