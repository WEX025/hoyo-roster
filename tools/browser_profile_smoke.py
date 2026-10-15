"""Offline shared-account browser persistence in a disposable Profile."""

import argparse
import asyncio
import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from hoyo_roster import platform_io as io
from hoyo_roster.auth import account_browser
from hoyo_roster.storage.common import secure_private_directory

URL = "https://hoyo-profile.invalid/"


async def check():
    temporary = io.temporary_root()
    root = io.filesystem_path(Path(tempfile.mkdtemp(prefix="hoyo-browser-profile-", dir=temporary)))
    completed = False
    try:
        for first, second in ((True, True), (True, False), (False, True)):
            mode = f"{'headed' if first else 'headless'}_to_{'headed' if second else 'headless'}"
            profile = secure_private_directory(root / mode)
            async with account_browser(profile, headed=first) as context:
                await context.route(
                    "**/*", lambda route: route.fulfill(body="<!doctype html>offline")
                )
                page = await context.new_page()
                await page.goto(URL)
                await context.add_cookies(
                    [
                        {
                            "name": "synthetic_persistent",
                            "value": "synthetic",
                            "url": URL,
                            "expires": time.time() + 3600,
                        },
                    ]
                )
                await page.evaluate("localStorage.setItem('synthetic_marker', 'synthetic')")
                await page.close()
                other_game = await context.new_page()
                await other_game.goto(URL)
                assert (
                    await other_game.evaluate("localStorage.getItem('synthetic_marker')")
                    == "synthetic"
                )
                await other_game.close()
            async with account_browser(profile, headed=second) as context:
                await context.route(
                    "**/*", lambda route: route.fulfill(body="<!doctype html>offline")
                )
                names = {item["name"] for item in await context.cookies(URL)}
                assert "synthetic_persistent" in names
                page = await context.new_page()
                await page.goto(URL)
                assert (
                    await page.evaluate("localStorage.getItem('synthetic_marker')") == "synthetic"
                )
            assert io.private(profile)
            print(f"SHARED_PROFILE_{mode.upper()}=PASS", flush=True)
        completed = True
    finally:
        if completed:
            io.check_components(root)
            if root.parent != temporary or not root.name.startswith("hoyo-browser-profile-"):
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
