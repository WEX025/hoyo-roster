"""Check a known official agreement only after separate user authorization."""

import asyncio
import re
from time import monotonic
from urllib.parse import urljoin, urlsplit

from playwright.async_api import Error as PlaywrightError

from .login_ui import sdk_frame, visible_frames
from .progress import emit

TERMS_TEXT = "已阅读并同意《米哈游用户协议》《米哈游隐私政策》，未注册的手机号验证通过将自动注册。"
TERMS_LINKS = {"/agreement?hideBack=true", "/privacyAgreement?hideBack=true"}


async def known_terms_checkbox(frame):
    agreement = frame.locator(".agree-checkbox")
    checkbox = agreement.get_by_role("checkbox")
    if (
        await agreement.count() != 1
        or not await agreement.is_visible()
        or await checkbox.count() != 1
        or not await checkbox.is_visible()
    ):
        return None
    text = re.sub(r"\s+", "", await agreement.inner_text())
    links = await agreement.locator("a").all()
    targets = set()
    for link in links:
        url = urlsplit(urljoin(frame.url, await link.get_attribute("href") or ""))
        if (
            url.scheme != "https"
            or url.hostname != "user.mihoyo.com"
            or url.port not in (None, 443)
            or url.username is not None
            or url.password is not None
            or url.path != "/"
            or url.query
        ):
            return None
        targets.add(url.fragment)
    if text != TERMS_TEXT or len(links) != 2 or targets != TERMS_LINKS:
        return None
    return checkbox


class TermsLoginAction:
    """Created only with explicit terms permission; never toggle or retry a check."""

    def __init__(self):
        self.finished = False
        self.failed = False
        self.attempted = False

    def handoff(self):
        if not self.finished:
            emit("TERMS_AUTOMATION_MANUAL")
        self.finished = self.failed = True

    async def accept(self, checkbox):
        if self.finished:
            return not self.failed
        if not await checkbox.is_checked():
            self.attempted = True
            await checkbox.check(timeout=1500)
            if not await checkbox.is_checked():
                self.handoff()
                return False
            emit("TERMS_ACCEPTED")
        self.finished = True
        return True

    async def step(self, page, *, captcha_wait, deadline):
        if self.finished:
            return
        await captcha_wait.ready(page, deadline)
        remaining = deadline - monotonic()
        if remaining <= 0:
            return  # The owning login loop reports the shared timeout.
        try:
            async with asyncio.timeout(min(2, remaining)):
                frames = [frame for frame in await visible_frames(page) if sdk_frame(frame)]
                if not frames:
                    return  # Login may already have completed and removed its SDK.
                if len(frames) != 1:
                    self.handoff()
                    return
                frame = frames[0]
                if await frame.locator(".agree-checkbox").count() == 0:
                    return  # Some QR entrypoints have no agreement control.
                checkbox = await known_terms_checkbox(frame)
                if checkbox is None:
                    self.handoff()
                    return
                await self.accept(checkbox)
        except (TimeoutError, PlaywrightError, ValueError):
            self.handoff()
