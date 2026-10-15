"""Check a known official agreement only after separate user authorization."""

import asyncio
import re
from time import monotonic
from urllib.parse import parse_qs, urljoin, urlsplit

from playwright.async_api import Error as PlaywrightError

from .login_ui import sdk_frame, visible_frames
from .progress import emit
from .login_controls import ControlReadiness

TERMS_TEXT = "已阅读并同意《米哈游用户协议》《米哈游隐私政策》，未注册的手机号验证通过将自动注册。"
TERMS_PATHS = {"/agreement", "/privacyAgreement"}
TERMS_TITLES = {"《米哈游用户协议》", "《米哈游隐私政策》"}


class StyledCheckbox:
    """Read native state; operate the visible SDK indicator when the input is hidden."""

    def __init__(self, checkbox, indicator):
        self.checkbox = checkbox
        self.indicator = indicator

    async def is_checked(self):
        return await self.checkbox.is_checked()

    async def check(self, *, timeout):
        if not await self.is_checked():
            await self.indicator.click(timeout=timeout)


async def known_terms_checkbox(frame, *, on_missing=None):
    def unavailable(reason):
        if on_missing is not None:
            on_missing(reason)
        return None

    agreement = frame.locator(".agree-checkbox")
    checkbox = agreement.get_by_role("checkbox")
    if await agreement.count() != 1 or not await agreement.is_visible():
        return unavailable("TERMS_CONTAINER_NOT_READY")
    if await checkbox.count() != 1:
        return unavailable("TERMS_CHECKBOX_NOT_READY_OR_AMBIGUOUS")
    # Wording, punctuation, QR's absent registration note and presentation flags
    # do not change the two authorized agreements. Additional obligations still do.
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
            return unavailable("TERMS_LINK_NOT_RECOGNIZED")
        target = urlsplit(url.fragment)
        if target.path not in TERMS_PATHS or target.fragment:
            return unavailable("TERMS_LINK_NOT_RECOGNIZED")
        # Only a documented UI presentation flag is irrelevant to consent.
        if set(parse_qs(target.query, keep_blank_values=True)) - {"hideBack"}:
            return unavailable("TERMS_LINK_NOT_RECOGNIZED")
        targets.add(target.path)
    if len(links) != 2 or targets != TERMS_PATHS or not all(t in text for t in TERMS_TITLES):
        return unavailable("TERMS_DOCUMENTS_NOT_RECOGNIZED")
    for title in TERMS_TITLES:
        text = text.replace(title, "", 1)
    text = re.sub(r"^(?:我|您|请您)?(?:已)?阅读并同意", "", text)
    text = text.replace("未注册的手机号验证通过将自动注册", "", 1)
    if re.sub(r"[\s，,。.;；:：、]", "", text):
        return unavailable("TERMS_EXTRA_TEXT")  # Additional obligations need consent.
    if await checkbox.is_visible():
        return checkbox
    indicator = agreement.locator(".el-checkbox__inner")
    if await indicator.count() == 1 and await indicator.is_visible():
        return StyledCheckbox(checkbox, indicator)
    return unavailable("TERMS_CHECKBOX_NOT_ACTIONABLE")


class TermsLoginAction:
    """Created only with explicit terms permission; never toggle or retry a check."""

    def __init__(self):
        self.finished = False
        self.failed = False
        self.attempted = False
        self.readiness = ControlReadiness("TERMS")

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
                matches = [
                    frame for frame in frames if await frame.locator(".agree-checkbox").is_visible()
                ]
                if len(matches) != 1:
                    if self.readiness.waiting("TERMS_PANEL_NOT_READY"):
                        self.handoff()
                    return
                reasons = []
                checkbox = await known_terms_checkbox(matches[0], on_missing=reasons.append)
                if checkbox is None:
                    if self.readiness.waiting(reasons[0]):
                        self.handoff()
                    return
                self.readiness.ready()
                await self.accept(checkbox)
        except (TimeoutError, PlaywrightError, ValueError):
            if self.attempted or self.readiness.waiting("TERMS_CONTROL_UNAVAILABLE"):
                self.handoff()
