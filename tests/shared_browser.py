"""Adapt existing synthetic browser surfaces to the account-owned lifetime."""

from contextlib import asynccontextmanager
from hoyo_roster import auth
from hoyo_roster.validation import CONTRACT_ROOT


def install(monkeypatch, manager):
    @asynccontextmanager
    async def browser(profile, *, headed):
        async with manager(
            repo_root=CONTRACT_ROOT, browser_profile=profile, headed=headed
        ) as session:
            context = session.context
            if hasattr(session.page, "business_requests"):
                from tests.test_direct_entrypoint import BusinessPage

                async def new_page():
                    page = BusinessPage(session.page.uid)
                    context.pages.append(page)
                    return page

                context.new_page = new_page
            yield context

    monkeypatch.setattr(auth, "account_browser", browser)
