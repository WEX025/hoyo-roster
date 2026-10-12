"""Official page navigation without credentials or recording."""

from playwright.async_api import BrowserContext, Page
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from .errors import ContextNotReusable, LoginNetworkError

type BrowserContextAccess = BrowserContext


async def navigate_login_page(page: Page, url: str) -> None:
    try:
        await page.goto(url, wait_until="commit")
        await page.bring_to_front()
    except PlaywrightError as error:
        if page.is_closed():
            raise ContextNotReusable("Login page unavailable") from None
        if isinstance(error, PlaywrightTimeoutError) or "net::ERR_" in str(error):
            raise LoginNetworkError("Official login page unavailable") from None
        raise
