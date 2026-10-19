"""Login ends at human confirmation. Identity and data checks belong to games."""

from dataclasses import dataclass, field

from .client import ClientSession
from .errors import ClientProtocolError, LoginCancelled
from .progress import emit

LOGIN_URL = "https://act.mihoyo.com/ys/event/calculator/index.html#/character"
LOGIN_QUESTION = "请在已打开的官方页面自行完成登录。你是否已完成登录？"


@dataclass(frozen=True, repr=False)
class LoginHandoff:
    session: ClientSession = field(repr=False)
    page_ref: str = field(repr=False)
    user_confirmed: bool = True


async def login(session: ClientSession) -> LoginHandoff:
    opened = await session.call("login.open", {"url": LOGIN_URL})
    if (
        not isinstance(opened, dict)
        or set(opened) != {"page"}
        or not isinstance(opened["page"], str)
        or not opened["page"]
    ):
        raise ClientProtocolError()
    emit("WAITING_FOR_USER")
    while True:
        answer = await session.call(
            "login.confirm", {"page": opened["page"], "question": LOGIN_QUESTION}
        )
        if answer == "cancel":
            raise LoginCancelled()
        if answer == "waiting":
            continue
        if answer != "completed":
            raise ClientProtocolError()
        emit("LOGIN_COMPLETED")
        return LoginHandoff(session, opened["page"])
