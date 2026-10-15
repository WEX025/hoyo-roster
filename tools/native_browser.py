"""Run offline native checks with one installed browser, stopping on first failure."""

import argparse
import os
from pathlib import Path
import subprocess
import sys

from hoyo_roster.browser_runtime import CHANNELS, preferred_channel
from hoyo_roster.storage.common import atomic_write_metadata, secure_private_directory

ROOT = Path(__file__).resolve().parents[1]


class BrowserNotInstalled(Exception):
    pass


def system_executables(channel):
    """Locations used by the pinned Playwright 1.54 channel registry; no launch."""
    if sys.platform == "darwin":
        app = {"chrome": "Google Chrome", "msedge": "Microsoft Edge"}[channel]
        return (Path(f"/Applications/{app}.app/Contents/MacOS/{app}"),)
    if sys.platform == "win32":
        relative = {
            "chrome": "Google/Chrome/Application/chrome.exe",
            "msedge": "Microsoft/Edge/Application/msedge.exe",
        }[channel]
        return tuple(
            Path(os.environ[name]) / relative
            for name in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)")
            if os.environ.get(name)
        )
    return (
        Path(
            {"chrome": "/opt/google/chrome/chrome", "msedge": "/opt/microsoft/msedge/msedge"}[
                channel
            ]
        ),
    )


def installed(channel):
    if channel == "chromium":
        from playwright.sync_api import sync_playwright

        # Starts only the Playwright driver to obtain its configured executable path.
        with sync_playwright() as playwright:
            return Path(playwright.chromium.executable_path).is_file()
    return any(path.is_file() and os.access(path, os.X_OK) for path in system_executables(channel))


def choose_channel(requested=None):
    requested = requested or os.environ.get("HOYO_TEST_BROWSER_CHANNEL", "auto")
    if requested not in ("auto", *CHANNELS):
        raise BrowserNotInstalled("NATIVE_BROWSER_CHANNEL_INVALID")
    choices = (
        tuple(dict.fromkeys((preferred_channel(), "chromium")))
        if requested == "auto"
        else (requested,)
    )
    for channel in choices:
        if installed(channel):
            return channel
    raise BrowserNotInstalled("NATIVE_BROWSER_NOT_INSTALLED")


def prepare_profile(profile, channel):
    """Pin only a fresh synthetic profile so launch failures cannot change engines."""
    profile = secure_private_directory(profile)
    if any(profile.iterdir()):
        raise ValueError("NATIVE_TEST_PROFILE_NOT_EMPTY")
    secure_private_directory(profile / channel)
    atomic_write_metadata(profile / "browser-choice.json", {"version": 1, "channel": channel})
    return profile


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", choices=("auto", *CHANNELS), default=None)
    parser.add_argument("--suite", choices=("login", "profile"), default="login")
    parser.add_argument(
        "--check-only", action="store_true", help="check installation without launching a browser"
    )
    args = parser.parse_args(argv)
    try:
        channel = choose_channel(args.channel)
    except BrowserNotInstalled as error:
        print(f"NATIVE_CHECK=BLOCKED\nERROR_CODE={error}", flush=True)
        return 2
    print(f"NATIVE_BROWSER_CHANNEL={channel}\nNATIVE_BROWSER_EXECUTABLE=FOUND", flush=True)
    if args.check_only:
        print("BROWSER_LAUNCH=NOT_RUN", flush=True)
        return 0
    environment = os.environ.copy()
    environment["HOYO_TEST_BROWSER_CHANNEL"] = channel
    environment["HOYO_NATIVE_LOGIN_SMOKE"] = "1"
    command = (
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-x",
            "tests/test_login_dom.py",
            "tests/test_terminal.py",
        ]
        if args.suite == "login"
        else [sys.executable, "tools/browser_profile_smoke.py", "--channel", channel]
    )
    # Use a terminal/environment already allowed to start system browsers. This
    # helper cannot grant sandbox permissions and never retries a failed launch.
    result = subprocess.run(command, cwd=ROOT, env=environment)
    print(f"NATIVE_CHECK={'PASS' if result.returncode == 0 else 'FAIL'}", flush=True)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
