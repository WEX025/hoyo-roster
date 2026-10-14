"""Offline selected-browser persistence in new disposable synthetic Profiles."""

import asyncio
import argparse
import importlib
import hashlib
import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from hoyo_roster import platform_io as io
from hoyo_roster.cli import CONTRACT_ROOT
from hoyo_roster.storage.common import secure_private_directory

URL = "https://hoyo-profile.invalid/"


async def open_synthetic_page(session):
    await session.context.route(
        "**/*", lambda route: route.fulfill(body="<!doctype html>synthetic offline control")
    )
    await session.page.goto(URL)


async def verify_session(browser, profile, *, game, headed):
    async with browser.persistent_browser(
        repo_root=CONTRACT_ROOT, browser_profile=profile, headed=headed
    ) as session:
        await open_synthetic_page(session)
        names = {item["name"] for item in await session.context.cookies(URL)}
        if "synthetic_persistent" not in names:
            raise AssertionError("synthetic persistent cookie was lost")
        if game == "genshin" and "synthetic_session" not in names:
            raise AssertionError("synthetic restored session cookie was lost")
        if await session.page.evaluate("localStorage.getItem('synthetic_marker')") != "synthetic":
            raise AssertionError("synthetic local storage was lost")


async def check():
    root = io.filesystem_path(Path(tempfile.mkdtemp(prefix="hoyo-browser-profile-")))
    completed = False
    try:
        for game in ("genshin", "zzz"):
            browser = importlib.import_module(f"hoyo_roster.games.{game}.browser")
            for first, second in ((True, True), (True, False), (False, True)):
                mode = (
                    f"{'headed' if first else 'headless'}_to_{'headed' if second else 'headless'}"
                )
                profile = secure_private_directory(root / f"{game}-{mode}")
                async with browser.persistent_browser(
                    repo_root=CONTRACT_ROOT, browser_profile=profile, headed=first
                ) as session:
                    await open_synthetic_page(session)
                    await session.context.add_cookies(
                        [
                            {"name": "synthetic_session", "value": "synthetic", "url": URL},
                            {
                                "name": "synthetic_persistent",
                                "value": "synthetic",
                                "url": URL,
                                "expires": time.time() + 3600,
                            },
                        ]
                    )
                    await session.page.evaluate(
                        "localStorage.setItem('synthetic_marker', 'synthetic')"
                    )
                await verify_session(browser, profile, game=game, headed=second)
                print(f"PROFILE_REUSE_{game.upper()}_{mode.upper()}=PASS", flush=True)
                if first and second:
                    parent = secure_private_directory(
                        root / "profiles" / hashlib.sha256(game.encode()).hexdigest()
                    )
                    destination = parent / "browser-profile"
                    profile.rename(destination)
                    await verify_session(browser, destination, game=game, headed=False)
                    if not io.private(destination):
                        raise AssertionError("private Profile permissions changed")
            print(f"BROWSER_PROFILE_{game.upper()}=PASS", flush=True)
        completed = True
    finally:
        if completed:
            if root.parent != io.filesystem_path(
                Path(tempfile.gettempdir())
            ) or not root.name.startswith("hoyo-browser-profile-"):
                raise AssertionError("temporary scope was not proven")
            shutil.rmtree(root)
        else:
            print("BROWSER_PROFILE_TEMPORARY_SCOPE=RETAINED", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", choices=("auto", "chromium"), default="auto")
    options = parser.parse_args()
    try:
        if options.channel == "chromium":
            with patch("hoyo_roster.browser_runtime.preferred_channel", return_value="chromium"):
                asyncio.run(check())
        else:
            asyncio.run(check())
    except Exception:
        print("BROWSER_PROFILE_SMOKE=FAIL\nERROR_CODE=BROWSER_PROFILE_SMOKE_FAILED")
        raise SystemExit(1)
    print("BROWSER_PROFILE_SMOKE=PASS\nLIVE_LOGIN=NOT_RUN\nLIVE_COLLECTION=NOT_RUN")
