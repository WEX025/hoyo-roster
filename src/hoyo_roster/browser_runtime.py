"""Select a browser once per private profile; never switch an existing session."""

import os
import sys
from pathlib import Path

from playwright.async_api import Error as PlaywrightError

from . import platform_io as io
from .browser import chromium_profile_path
from .errors import BrowserProfileInvalid, BrowserUnavailable
from .storage.common import atomic_write_metadata, load_metadata, secure_private_directory

CHANNELS = ("msedge", "chrome", "chromium")
CHOICE_FILE = "browser-choice.json"
SINGLETONS = ("SingletonLock", "SingletonSocket", "SingletonCookie")


def preferred_channel() -> str:
    if sys.platform == "win32":
        return "msedge"
    if sys.platform == "darwin":
        return "chrome"
    return "chromium"


def profile_in_use(profile: Path) -> bool:
    # Inspect only fixed paths, including the old root layout, without reading
    # browser data. A stale marker is also reason to stop, never permission to delete.
    for root in (profile, *(profile / channel for channel in CHANNELS)):
        io.check_components(root)
        if any(os.path.lexists(root / name) for name in SINGLETONS):
            return True
    return False


def saved_channel(profile: Path) -> str | None:
    marker = profile / CHOICE_FILE
    try:
        if os.path.lexists(marker):
            value = load_metadata(marker, private=True)
            if (
                not isinstance(value, dict)
                or set(value) != {"version", "channel"}
                or type(value["version"]) is not int
                or value["version"] != 1
                or value["channel"] not in CHANNELS
            ):
                raise BrowserProfileInvalid("Browser selection metadata was invalid")
            return value["channel"]
        if any(profile.iterdir()):
            # Never infer an engine from or add a marker to an existing profile.
            raise BrowserProfileInvalid("Unmarked profile retained; use a new explicit auth root")
        return None
    except BrowserProfileInvalid:
        raise
    except (OSError, ValueError):
        raise BrowserProfileInvalid("Browser selection metadata was unavailable") from None


async def launch_profile(playwright, profile: Path, *, headed: bool, args=()):
    """Caller owns the account authentication lock and closes the returned context.

    First launch may fall back only before any official navigation. Each engine
    has its own directory; even a failed launch never shares files with the fallback.
    Pin before launch so interruption/unknown durability cannot reselect an engine.
    """
    selected = saved_channel(profile)
    channels = (selected,) if selected else tuple(dict.fromkeys((preferred_channel(), "chromium")))
    for index, channel in enumerate(channels):
        try:
            if selected and not os.path.lexists(profile / channel):
                raise BrowserProfileInvalid(
                    "Selected browser directory is missing; profile retained"
                )
            engine_profile = secure_private_directory(profile / channel)
            if selected is None:
                atomic_write_metadata(profile / CHOICE_FILE, {"version": 1, "channel": channel})
        except (OSError, ValueError):
            raise BrowserProfileInvalid("Browser selection could not be secured") from None
        if channel == "chromium" and not Path(playwright.chromium.executable_path).is_file():
            raise BrowserUnavailable("Install fallback with: python -m playwright install chromium")
        try:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=chromium_profile_path(engine_profile),
                headless=not headed,
                channel=channel,
                accept_downloads=False,
                handle_sigint=False,  # CLI cancellation owns/drains browser teardown.
                timeout=30_000,
                args=list(args),
            )
        except PlaywrightError:
            if selected is None and index + 1 < len(channels):
                print(
                    "BROWSER_FALLBACK=CHROMIUM\n提示=首选浏览器无法启动，尝试 Chromium 专用会话。"
                )
                continue
            raise BrowserUnavailable("Selected browser failed to start; profile retained") from None
        print(f"BROWSER_CHANNEL={channel}")
        return context
    raise BrowserUnavailable("No browser could be started")
