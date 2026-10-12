"""Choose and authorize manual login before opening its official page."""

import asyncio
import re
import sys
from time import monotonic
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from .errors import (
    AuthenticationRequired,
    LoginCancelled,
    LoginConsentRequired,
    LoginControlUnavailable,
    LoginTimeout,
)
from .progress import OPTIONS, emit

LOGIN_NOTICE = (
    "登录前说明：二维码需本人扫码并确认；手机号方式需本人在官方页面输入手机号、"
    "请求短信并输入验证码。允许本工具打开官方页面、等待本人登录、核验所选游戏/UID/区服，"
    "并在本机该身份独立 Profile 中保存及复用会话。浏览器与官方服务通信；"
    "工具不接收或输出凭据。必要的官方协议与隐私条款请本人阅读并在官方页面确认；"
    "程序不会自动勾选。若出现未知新协议、额外权限或风控，请暂停并另行确认，"
    "此次允许不涵盖它们。可随时 Ctrl-C 取消。"
)


async def choose_method() -> str | None:
    options = OPTIONS.get()
    if options is None:
        return None
    if options.method == "cancel":
        emit("CANCELLED")
        raise LoginCancelled("User cancelled login")
    if options.consent_given:
        return options.method
    print(LOGIN_NOTICE, flush=True)
    interactive = not options.non_interactive and sys.stdin.isatty()
    if options.method is None:
        if not interactive:
            raise AuthenticationRequired("Use --login-method qr|phone|cancel when login is needed")
        prompt = "允许上述操作并选择登录方式 [1 二维码/2 手机号/3 取消]："
    elif options.allow_login:
        options.consent_given = True
        return options.method
    elif not interactive:
        raise LoginConsentRequired("Review login notice and explicitly use --allow-login")
    else:
        name = "二维码" if options.method == "qr" else "手机号/短信验证码"
        prompt = f"允许上述操作并使用{name}登录？[y/N]："
    try:
        answer = (await asyncio.to_thread(input, prompt)).strip().lower()
    except EOFError:
        answer = ""
    if options.method is None:
        options.method = {"1": "qr", "2": "phone", "qr": "qr", "phone": "phone"}.get(answer)
    elif answer not in {"y", "yes"}:
        options.method = None
    if options.method is None:
        emit("CANCELLED")
        raise LoginCancelled("User cancelled login")
    options.consent_given = True
    return options.method


async def confirm_method_entry(page, method: str | None) -> None:
    if method is None:
        return
    emit("WAITING_FOR_ENTRY")
    print(
        "请本人打开官方登录面板，并选择"
        + ("二维码" if method == "qr" else "手机号/短信验证码")
        + "；手机号、验证码、扫码、协议阅读和确认均在官方页面完成。",
        flush=True,
    )
    pattern = re.compile(
        "扫码登录|二维码登录|扫描二维码|扫码进行登录"
        if method == "qr"
        else "手机号|手机验证码|短信登录|验证码登录"
    )
    options = OPTIONS.get()
    deadline = monotonic() + (options.timeout if options else 900)
    control_failed = False
    while True:
        if page.is_closed():
            raise LoginControlUnavailable("Official login page closed; reopen the command")
        remaining = deadline - monotonic()
        if remaining <= 0:
            if control_failed:
                raise LoginControlUnavailable("Official login entry control timed out")
            raise LoginTimeout("Selected official entry was not observed before deadline")
        try:
            # Read visibility only; never inspect credentials or automate authentication.
            async with asyncio.timeout(min(2.0, remaining)):
                for frame in getattr(page, "frames", (page,)):
                    if await frame.get_by_text(pattern).first.is_visible():
                        print(f"LOGIN_METHOD={method.upper()}\nLOGIN_ENTRY=OBSERVED", flush=True)
                        return
            control_failed = False
        except (TimeoutError, PlaywrightTimeoutError):
            if not control_failed:
                emit("CONTROL_RETRY")
            control_failed = True
        except PlaywrightError as error:
            if any(marker in str(error) for marker in ("errAETimeout", "timeoutReached")):
                if not control_failed:
                    emit("CONTROL_RETRY")
                control_failed = True
            else:
                raise LoginControlUnavailable("Official login entry control unavailable") from None
        await asyncio.sleep(min(0.2, max(0, deadline - monotonic())))
