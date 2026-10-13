"""Native, synthetic Profile reuse across visible/headless modes; no official requests."""

from __future__ import annotations
import asyncio
import importlib
import tempfile
import time
from pathlib import Path


async def check() -> None:
    for game in ("genshin", "zzz"):
        browser = importlib.import_module(f"hoyo_roster.games.{game}.browser")
        for first, second in ((True, True), (True, False), (False, True)):
            with tempfile.TemporaryDirectory(prefix="hoyo-browser-profile-") as temporary:
                root = Path(temporary).resolve()
                profile = root / "profile"
                async with browser.persistent_browser(
                    repo_root=root / "workspace", browser_profile=profile, headed=first
                ) as session:
                    await session.page.goto("about:blank")
                    # Synthetic values only. Preserve each game's existing native session semantics.
                    await session.context.add_cookies(
                        [
                            {
                                "name": "synthetic_persistent",
                                "value": "synthetic",
                                "url": "https://example.invalid/",
                                "expires": time.time() + 3600,
                            },
                            {
                                "name": "synthetic_session",
                                "value": "synthetic",
                                "url": "https://example.invalid/",
                            },
                        ]
                    )
                async with browser.persistent_browser(
                    repo_root=root / "workspace", browser_profile=profile, headed=second
                ) as session:
                    cookies = await session.context.cookies("https://example.invalid/")
                    names = {item["name"] for item in cookies}
                    if "synthetic_persistent" not in names:
                        raise RuntimeError("SYNTHETIC_PERSISTENT_COOKIE_LOST")
                    if game == "genshin" and "synthetic_session" not in names:
                        raise RuntimeError("SYNTHETIC_RESTORED_SESSION_LOST")
                mode = (
                    ("headed" if first else "headless")
                    + "_to_"
                    + ("headed" if second else "headless")
                )
                print(f"PROFILE_REUSE_{game.upper()}_{mode.upper()}=PASS", flush=True)
    print("BROWSER_PROFILE_SMOKE=PASS\nLIVE_LOGIN=NOT_RUN\nLIVE_COLLECTION=NOT_RUN")


if __name__ == "__main__":
    asyncio.run(check())
