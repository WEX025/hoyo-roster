"""Persistent Chromium context with fixed Profile and recording disabled."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar, cast

from playwright.async_api import APIRequestContext, APIResponse, BrowserContext, Cookie, Page
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .errors import ContextNotReusable, LoginNetworkError
from .redaction import diagnostic_hash

if TYPE_CHECKING:
    from .request_gate import RequestGate


_T = TypeVar("_T")


class BorrowedAPIRequestContext:
    """Read transport without disposal or storage-state export authority."""

    __slots__ = ("__request",)

    def __init__(self, request: APIRequestContext) -> None:
        self.__request = request

    async def get(self, url: str, **kwargs: Any) -> APIResponse:
        return await self.__request.get(url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> APIResponse:
        return await self.__request.post(url, **kwargs)


class _BorrowedPage:
    """Delegate Page operations while hiding its raw BrowserContext escape hatch."""

    __slots__ = ("__page",)
    _BLOCKED_ATTRIBUTES = frozenset({"browser", "context", "playwright"})

    def __init__(self, page: Page) -> None:
        self.__page = page

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or name in self._BLOCKED_ATTRIBUTES:
            raise AttributeError(name)
        return getattr(self.__page, name)

    def __repr__(self) -> str:
        return "BorrowedPage()"


@dataclass(frozen=True, slots=True)
class JobResourceResidue:
    page_count: int
    route_count: int
    listener_count: int
    background_task_count: int

    @property
    def clean(self) -> bool:
        return (
            self.page_count
            == self.route_count
            == self.listener_count
            == self.background_task_count
            == 0
        )


class _JobResourceLedger:
    """Owner-side accounting for every shared resource one Job may create."""

    __slots__ = (
        "_active",
        "_background_tasks",
        "_baseline_page_ids",
        "_baseline_pages",
        "_context",
        "_listeners",
        "_routes",
        "_tainted",
    )

    def __init__(self, context: BrowserContext) -> None:
        self._context = context
        self._baseline_pages = tuple(
            page for page in getattr(context, "pages", ()) if _page_is_open(page)
        )
        self._baseline_page_ids = {id(page) for page in self._baseline_pages}
        self._routes: list[tuple[Any, Any]] = []
        self._listeners: list[tuple[str, Any]] = []
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._tainted = False
        self._active = True

    def require_active(self) -> None:
        if not self._active:
            raise ContextNotReusable("Job browser capability is no longer active")

    def track_route(self, pattern: Any, handler: Any) -> None:
        self.require_active()
        self._routes.append((pattern, handler))

    def untrack_route(self, pattern: Any, handler: Any | None) -> None:
        self.require_active()
        if handler is None:
            self._routes = [entry for entry in self._routes if entry[0] != pattern]
            return
        with_value = (pattern, handler)
        if with_value in self._routes:
            self._routes.remove(with_value)

    def track_listener(self, event: str, handler: Any) -> None:
        self.require_active()
        self._listeners.append((event, handler))

    def untrack_listener(self, event: str, handler: Any) -> None:
        self.require_active()
        with_value = (event, handler)
        if with_value in self._listeners:
            self._listeners.remove(with_value)

    def track_background_task(self, task: asyncio.Task[_T]) -> asyncio.Task[_T]:
        self.require_active()
        self._background_tasks.add(task)
        return task

    def mark_tainted(self) -> None:
        self._tainted = True

    async def _cleanup_background_tasks(self) -> tuple[int, bool]:
        tasks = set(self._background_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if not tasks:
            return 0, False
        done, pending = await asyncio.wait(tasks, timeout=1.0)
        failed = bool(pending)
        for task in done:
            if task.cancelled():
                continue
            try:
                error = task.exception()
            except asyncio.CancelledError:
                continue
            if error is not None:
                failed = True
        self._background_tasks = set(pending)
        return len(pending), failed

    def _page_residue_count(self) -> int:
        pages = [page for page in getattr(self._context, "pages", ()) if _page_is_open(page)]
        unexpected = sum(id(page) not in self._baseline_page_ids for page in pages)
        missing = sum(not _page_is_open(page) for page in self._baseline_pages)
        return unexpected + missing

    async def cleanup(self) -> JobResourceResidue:
        self.require_active()
        cleanup_failed = self._tainted
        cleanup_cancelled: asyncio.CancelledError | None = None
        try:
            background_count, background_failed = await self._cleanup_background_tasks()
        except asyncio.CancelledError as error:
            cleanup_cancelled = error
            cleanup_failed = True
            background_count = sum(not task.done() for task in self._background_tasks)
        except BaseException:
            cleanup_failed = True
            background_count = sum(not task.done() for task in self._background_tasks)
        else:
            cleanup_failed = cleanup_failed or background_failed

        for event, handler in reversed(self._listeners.copy()):
            try:
                self._context.remove_listener(event, handler)
            except asyncio.CancelledError as error:
                cleanup_failed = True
                if cleanup_cancelled is None:
                    cleanup_cancelled = error
            except BaseException:
                cleanup_failed = True
            else:
                self._listeners.remove((event, handler))

        for pattern, handler in reversed(self._routes.copy()):
            try:
                await self._context.unroute(pattern, handler)
            except asyncio.CancelledError as error:
                cleanup_failed = True
                if cleanup_cancelled is None:
                    cleanup_cancelled = error
            except BaseException:
                cleanup_failed = True
            else:
                self._routes.remove((pattern, handler))

        job_pages = [
            page
            for page in getattr(self._context, "pages", ())
            if _page_is_open(page) and id(page) not in self._baseline_page_ids
        ]
        for page in job_pages:
            try:
                await page.goto("about:blank", wait_until="commit")
            except asyncio.CancelledError as error:
                cleanup_failed = True
                if cleanup_cancelled is None:
                    cleanup_cancelled = error
            except BaseException:
                cleanup_failed = True
            try:
                await page.close()
            except asyncio.CancelledError as error:
                cleanup_failed = True
                if cleanup_cancelled is None:
                    cleanup_cancelled = error
            except BaseException:
                cleanup_failed = True

        residue = JobResourceResidue(
            page_count=self._page_residue_count(),
            route_count=len(self._routes),
            listener_count=len(self._listeners),
            background_task_count=background_count,
        )
        self._active = False
        if cleanup_cancelled is not None:
            raise cleanup_cancelled
        if cleanup_failed or not residue.clean:
            raise ContextNotReusable("Job browser resource cleanup was incomplete")
        return residue


class BorrowedBrowserContext:
    """Job-scoped browser capability without Owner lifecycle authority."""

    __slots__ = ("__context", "__ledger", "__request")

    def __init__(
        self,
        context: BrowserContext,
        *,
        _ledger: _JobResourceLedger | None = None,
    ) -> None:
        self.__context = context
        self.__ledger = _ledger if _ledger is not None else _JobResourceLedger(context)
        self.__request: BorrowedAPIRequestContext | None = None

    @property
    def request(self) -> BorrowedAPIRequestContext:
        self.__ledger.require_active()
        if self.__request is None:
            self.__request = BorrowedAPIRequestContext(self.__context.request)
        return self.__request

    async def new_page(self) -> Page:
        self.__ledger.require_active()
        page = await self.__context.new_page()
        return cast(Page, _BorrowedPage(page))

    async def route(self, *args: Any, **kwargs: Any) -> None:
        self.__ledger.require_active()
        await self.__context.route(*args, **kwargs)
        pattern = args[0] if args else kwargs.get("url")
        handler = args[1] if len(args) > 1 else kwargs.get("handler")
        self.__ledger.track_route(pattern, handler)

    async def unroute(self, *args: Any, **kwargs: Any) -> None:
        self.__ledger.require_active()
        await self.__context.unroute(*args, **kwargs)
        pattern = args[0] if args else kwargs.get("url")
        handler = args[1] if len(args) > 1 else kwargs.get("handler")
        self.__ledger.untrack_route(pattern, handler)

    def on(self, *args: Any, **kwargs: Any) -> None:
        self.__ledger.require_active()
        self.__context.on(*args, **kwargs)
        event = args[0] if args else kwargs.get("event")
        handler = args[1] if len(args) > 1 else kwargs.get("f")
        if not isinstance(event, str):
            raise TypeError("browser listener event was invalid")
        self.__ledger.track_listener(event, handler)

    def remove_listener(self, *args: Any, **kwargs: Any) -> None:
        self.__ledger.require_active()
        self.__context.remove_listener(*args, **kwargs)
        event = args[0] if args else kwargs.get("event")
        handler = args[1] if len(args) > 1 else kwargs.get("f")
        if not isinstance(event, str):
            raise TypeError("browser listener event was invalid")
        self.__ledger.untrack_listener(event, handler)

    async def cookies(self, *args: Any, **kwargs: Any) -> list[Cookie]:
        self.__ledger.require_active()
        return await self.__context.cookies(*args, **kwargs)

    def create_task(
        self,
        coroutine: Coroutine[Any, Any, _T],
    ) -> asyncio.Task[_T]:
        self.__ledger.require_active()
        task = asyncio.create_task(coroutine)
        return self.__ledger.track_background_task(task)

    def _record_cleanup_failure(self) -> None:
        self.__ledger.mark_tainted()

    def _require_active(self) -> None:
        self.__ledger.require_active()


class LoginBrowserContext(BorrowedBrowserContext):
    """Elevated context capability available only to the Login Coordinator."""

    __slots__ = ("__login_context",)

    def __init__(
        self,
        context: BrowserContext,
        *,
        _ledger: _JobResourceLedger,
    ) -> None:
        super().__init__(context, _ledger=_ledger)
        self.__login_context = context

    async def clear_cookies(self, *args: Any, **kwargs: Any) -> None:
        self._require_active()
        await self.__login_context.clear_cookies(*args, **kwargs)


type BrowserContextAccess = BrowserContext | BorrowedBrowserContext


async def navigate_login_page(page: Page, url: str) -> None:
    """Distinguish a failed official navigation from a lost browser Context."""

    try:
        await page.goto(url, wait_until="commit")
        await page.bring_to_front()
    except PlaywrightError as error:
        if page.is_closed():
            raise ContextNotReusable("Login Page is unavailable") from None
        if isinstance(error, PlaywrightTimeoutError) or "net::ERR_" in str(error):
            raise LoginNetworkError("Official login page could not be reached") from None
        raise


@dataclass(frozen=True, slots=True)
class BorrowedBrowserSession:
    """The Owner's restricted browser capability borrowed by one serial Job."""

    context: BorrowedBrowserContext = field(repr=False)
    request_gate: RequestGate = field(repr=False)
    context_generation: str
    headed: bool = True


@dataclass(frozen=True, slots=True)
class LoginBrowserSession:
    """Owner-only authority used for the explicit interactive login operation."""

    context: LoginBrowserContext = field(repr=False)
    idle_page: Page = field(repr=False)
    request_gate: RequestGate = field(repr=False)
    context_generation: str
    headed: bool = True


type BrowserOwnerSession = BorrowedBrowserSession | LoginBrowserSession


class BrowserJobLease:
    """Create one capability and require explicit cleanup proof before reuse."""

    __slots__ = (
        "_context",
        "_generation",
        "_ledger",
        "_login_context",
        "_request_gate",
    )

    def __init__(
        self,
        context: BrowserContext,
        *,
        request_gate: RequestGate,
        context_generation: str,
    ) -> None:
        self._ledger = _JobResourceLedger(context)
        self._context = BorrowedBrowserContext(context, _ledger=self._ledger)
        self._login_context = LoginBrowserContext(context, _ledger=self._ledger)
        self._request_gate = request_gate
        self._generation = context_generation

    def borrow(self) -> BorrowedBrowserSession:
        return BorrowedBrowserSession(
            context=self._context,
            request_gate=self._request_gate,
            context_generation=self._generation,
        )

    def borrow_for_login(self, idle_page: Page) -> LoginBrowserSession:
        return LoginBrowserSession(
            context=self._login_context,
            idle_page=idle_page,
            request_gate=self._request_gate,
            context_generation=self._generation,
        )

    async def cleanup(self) -> JobResourceResidue:
        return await self._ledger.cleanup()


def _cleanup_error(
    error: BaseException,
    *,
    context: str,
) -> ContextNotReusable:
    diagnostic = diagnostic_hash(error, context=context)
    return ContextNotReusable(f"Browser cleanup failed; diagnostic_sha256={diagnostic[:12]}")


def _page_is_open(page: Page) -> bool:
    checker = getattr(page, "is_closed", None)
    return not bool(checker()) if callable(checker) else True


async def seal_context_pages(
    context: BrowserContext,
    idle_page: Page,
) -> None:
    """Leave exactly one inert Page without closing the borrowed Context."""

    pages = [page for page in context.pages if _page_is_open(page)]
    if idle_page not in pages:
        raise ContextNotReusable("Owner idle Page is unavailable")
    ordered = [idle_page, *(page for page in pages if page is not idle_page)]
    try:
        for page in ordered:
            await page.goto("about:blank", wait_until="commit")
        for page in ordered[1:]:
            await page.close()
    except PlaywrightError as error:
        raise _cleanup_error(error, context="owner-page-seal") from None
    remaining = [page for page in context.pages if _page_is_open(page)]
    if remaining != [idle_page]:
        raise ContextNotReusable("Owner Page cleanup did not reach one idle Page")


@asynccontextmanager
async def borrowed_job_page(
    browser: BorrowedBrowserSession,
) -> AsyncIterator[Page]:
    """Create and clean one Page without changing Owner lifecycle authority."""

    page: Page | None = None
    primary_error: BaseException | None = None
    try:
        page = await browser.context.new_page()
        yield page
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if page is not None and _page_is_open(page):
            try:
                await page.goto("about:blank", wait_until="commit")
                await page.close()
            except BaseException as error:
                record_failure = getattr(
                    browser.context,
                    "_record_cleanup_failure",
                    None,
                )
                if callable(record_failure):
                    record_failure()
                elif primary_error is None:
                    if isinstance(error, asyncio.CancelledError):
                        raise
                    raise _cleanup_error(
                        error,
                        context="borrowed-job-page",
                    ) from None
