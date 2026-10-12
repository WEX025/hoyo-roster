"""Shared local Hoyo browser-session broker foundation."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .login import coordinate_login
    from .models import JobParameters
    from .queue import mark_login_verified, mark_login_waiting, submit_and_wait

__all__ = [
    "JobParameters",
    "coordinate_login",
    "mark_login_verified",
    "mark_login_waiting",
    "submit_and_wait",
]
__version__ = "0.14.0"


def __getattr__(name: str) -> object:
    if name == "JobParameters":
        from .models import JobParameters

        return JobParameters
    if name == "coordinate_login":
        from .login import coordinate_login

        return coordinate_login
    if name in {"mark_login_verified", "mark_login_waiting", "submit_and_wait"}:
        from .queue import mark_login_verified, mark_login_waiting, submit_and_wait

        return {
            "mark_login_verified": mark_login_verified,
            "mark_login_waiting": mark_login_waiting,
            "submit_and_wait": submit_and_wait,
        }[name]
    raise AttributeError(name)
