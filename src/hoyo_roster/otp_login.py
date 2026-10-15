"""Ephemeral, once-only SMS code entry in the same inspected official login form."""

import asyncio
import re
from time import monotonic

from playwright.async_api import Error as PlaywrightError

from .errors import LoginTimeout, OtpInputInvalid
from .login_ui import sdk_frame, visible_frames
from .progress import emit
from .login_controls import ControlReadiness
from .terminal import read_terminal
from .terms_login import known_terms_checkbox

FORM = "form.captcha-login-page:visible"
CODE_INPUT = '.mhy-cmn-captcha-input input:visible, input[autocomplete="one-time-code"]:visible'


async def read_otp():
    try:
        code = await read_terminal(
            "本次短信验证码（6 位，隐藏输入，不保存；Ctrl-C 取消）：", hidden=True
        )
    except (EOFError, OSError, Warning):
        raise OtpInputInvalid("SMS code input unavailable") from None
    if re.fullmatch(r"[0-9]{6}", code) is None:
        code = None
        raise OtpInputInvalid("SMS code input invalid") from None
    return code


async def known_controls(page):
    frames = [frame for frame in await visible_frames(page) if sdk_frame(frame)]
    matches = [frame for frame in frames if await frame.locator(FORM).is_visible()]
    if len(matches) != 1:
        return None
    frame = matches[0]
    form = frame.locator(FORM)
    checkbox = await known_terms_checkbox(frame)
    if (
        await form.count() != 1
        or not await form.is_visible()
        or checkbox is None
        or not await checkbox.is_checked()
    ):
        return None
    field = form.locator(CODE_INPUT)
    submit = form.locator('button[type="submit"]:visible')
    if (
        await field.count() != 1
        or not await field.is_visible()
        or not await field.is_editable()
        or await submit.count() != 1
        or not await submit.is_visible()
        or re.sub(r"\s+", "", await submit.inner_text()) != "登录"
    ):
        return None
    return field, submit


class OtpLoginAction:
    def __init__(self, phone):
        self.phone = phone
        self.watch = phone.watch
        self.task = None
        self.finished = False
        self.attempted = False
        self.filled = False
        self.submitted = False
        self.readiness = ControlReadiness("OTP")

    async def close(self):
        self.finished = True
        if self.task is not None:
            task, self.task = self.task, None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def handoff(self):
        await self.close()
        emit("OTP_AUTOMATION_MANUAL")

    async def step(self, page, *, captcha_wait, deadline):
        if self.finished or self.phone.manual or not self.phone.attempted:
            return
        self.watch.check()
        # Cooldown/unknown/rejected receipts never request a historical code.
        if self.watch.state != "SMS_ACCEPTED":
            return
        await captcha_wait.ready(page, deadline)
        self.watch.check()
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise LoginTimeout("Official SMS code action deadline exceeded")
        code = None
        try:
            async with asyncio.timeout(min(2, remaining)):
                controls = await known_controls(page)
                if controls is None:
                    if self.readiness.waiting("OTP_FORM_NOT_READY_OR_UNRECOGNIZED"):
                        await self.handoff()
                    return
                if self.filled:
                    if not await controls[1].is_enabled():
                        if self.readiness.waiting("LOGIN_BUTTON_NOT_READY"):
                            await self.handoff()
                        return
                    self.readiness.ready()
                    self.submitted = self.finished = True
                    emit("LOGIN_SUBMITTING")
                    await controls[1].click(timeout=1500)
                    return
                self.readiness.ready()
                if self.task is None:
                    emit("OTP_INPUT_REQUIRED")
                    self.task = asyncio.create_task(read_otp())
                    return
                if not self.task.done():
                    return  # Keep CAPTCHA, receipt protection and Binding checks live.
                task, self.task = self.task, None
                code = task.result()
                del task
                self.watch.check()
                self.attempted = True  # Never repeat an uncertain fill/submit.
                field, _ = controls
                await field.fill(code, timeout=1500)
                code = None
                self.filled = True
                emit("OTP_FILLED")
                # Recheck CAPTCHA, receipt, terms and controls in the next owning loop.
        except (TimeoutError, PlaywrightError, ValueError):
            if self.attempted or self.readiness.waiting("OTP_CONTROL_UNAVAILABLE"):
                await self.handoff()
        finally:
            code = None
