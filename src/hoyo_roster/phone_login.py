"""Ephemeral phone fill, optional authorized terms check and one guarded SMS click."""

import asyncio
import re
from time import monotonic
from playwright.async_api import Error as PlaywrightError
from .errors import LoginTimeout, PhoneInputInvalid
from .login_ui import sdk_frame, visible_frames
from .progress import emit
from .terminal import read_terminal
from .terms_login import TERMS_TEXT as TERMS_TEXT, TermsLoginAction, known_terms_checkbox


async def read_phone() -> str:
    try:
        number = await read_terminal("临时手机号（隐藏输入，不保存；Ctrl-C 取消）：", hidden=True)
    except (EOFError, OSError, Warning):
        raise PhoneInputInvalid("Phone input unavailable") from None
    if re.fullmatch(r"1[3-9][0-9]{9}", number) is None:
        raise PhoneInputInvalid("Phone input invalid") from None
    return number


class PhoneLoginAction:
    """Only constructed after manual-login consent and explicit once-only SMS permission."""

    def __init__(self, number, watch, *, allow_terms=False):
        self._number = number
        self.watch = watch
        self.filled = False
        self.finished = False
        self.attempted = False
        self.waiting_for_terms = False
        self.manual = False
        self.terms = TermsLoginAction() if allow_terms else None

    def handoff(self, code="PHONE_AUTOMATION_MANUAL"):
        self.manual = True
        self._number = None
        self.finished = True
        emit(code)

    def authenticated(self):
        self._number = None
        self.finished = True

    async def step(self, page, *, captcha_wait, deadline):
        if self.finished:
            return
        if self.watch.request is not None:
            # A person may have submitted first. Do not fill or click after that.
            self._number = None
            self.finished = True
            return
        await captcha_wait.ready(page, deadline)
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise LoginTimeout("Official phone action deadline exceeded")
        try:
            async with asyncio.timeout(min(2, remaining)):
                frames = [frame for frame in await visible_frames(page) if sdk_frame(frame)]
                if len(frames) != 1:
                    self.handoff()
                    return
                frame = frames[0]
                mobile = frame.locator("input#mobile")
                checkbox = await known_terms_checkbox(frame)
                if await mobile.count() != 1 or not await mobile.is_visible() or checkbox is None:
                    self.handoff()
                    return
                if not self.filled:
                    self.filled = True  # uncertain fill is never automatically repeated
                    number, self._number = self._number, None
                    await mobile.fill(number, timeout=1500)
                    number = None
                    emit("PHONE_ENTERED")
                if not await checkbox.is_checked():
                    if self.terms is not None:
                        if not await self.terms.accept(checkbox):
                            self.handoff()
                            return
                    else:
                        if not self.waiting_for_terms:
                            emit("WAITING_FOR_TERMS")
                            self.waiting_for_terms = True
                        return
                send = frame.get_by_text("获取验证码", exact=True)
                if (
                    await send.count() != 1
                    or not await send.is_visible()
                    or not await send.is_enabled()
                ):
                    self.handoff()
                    return
                if self.watch.request is not None:
                    self.finished = True
                    return
                self.attempted = self.finished = True  # reserve before dispatch, never retry
                emit("SMS_SUBMITTING")
                await send.click(timeout=1500)
        except (TimeoutError, PlaywrightError, ValueError):
            if self.terms is not None and self.terms.attempted and not self.terms.finished:
                self.terms.handoff()
            self.handoff("SMS_ACTION_UNCERTAIN" if self.attempted else "PHONE_AUTOMATION_MANUAL")

    def close(self):
        self._number = None
        if self.attempted and self.watch.request is None:
            emit("SMS_REQUEST_NOT_OBSERVED")
