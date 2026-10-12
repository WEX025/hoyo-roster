"""Choose a method; all authentication remains manual on the official page."""

import asyncio
import re
import sys
from .errors import AuthenticationRequired, LoginCancelled, LoginMethodUnavailable
from .progress import OPTIONS, emit


async def choose_method() -> str | None:
    options = OPTIONS.get()
    if options is None:
        return None
    if options.method is None:
        if options.non_interactive or not sys.stdin.isatty():
            raise AuthenticationRequired(
                "Use --login-method qr|phone|cancel when a session needs login"
            )
        print(
            "登录方式：1 二维码 / 2 手机号 / 3 取消（仅在官方页面操作；终端不接收凭据）", flush=True
        )
        try:
            answer = await asyncio.to_thread(input, "请选择 [1/2/3]：")
        except EOFError:
            answer = "3"
        options.method = {
            "1": "qr",
            "2": "phone",
            "3": "cancel",
            "qr": "qr",
            "phone": "phone",
            "cancel": "cancel",
        }.get(answer.strip(), "cancel")
    if options.method == "cancel":
        emit("CANCELLED")
        raise LoginCancelled("User cancelled login")
    return options.method


async def confirm_method_entry(page, method: str | None) -> None:
    if method is None:
        return
    emit("WAITING_FOR_USER")
    print(
        "请本人打开官方登录面板，并选择"
        + ("二维码" if method == "qr" else "手机号/短信验证码")
        + "；手机号、验证码、扫码和确认均在官方页面完成。",
        flush=True,
    )
    pattern = re.compile(
        "扫码登录|二维码登录|扫描二维码|扫码进行登录"
        if method == "qr"
        else "手机号|手机验证码|短信登录|验证码登录"
    )
    options = OPTIONS.get()
    deadline = asyncio.get_running_loop().time() + min(30, options.timeout if options else 30)
    while True:
        # Read only visibility; never extract DOM credential values or automate authentication.
        for frame in getattr(page, "frames", (page,)):
            if await frame.get_by_text(pattern).first.is_visible():
                print(f"LOGIN_METHOD={method.upper()}\nLOGIN_ENTRY=OBSERVED", flush=True)
                return
        if asyncio.get_running_loop().time() >= deadline:
            emit("METHOD_UNAVAILABLE")
            raise LoginMethodUnavailable("Selected official method entry not observed")
        await asyncio.sleep(0.2)
