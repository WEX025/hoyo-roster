from hoyo_roster.auth import account_browser as REAL_ACCOUNT_BROWSER

"""Synthetic browser selection, startup fallback and persisted ownership boundaries."""

import asyncio
import os
from types import SimpleNamespace

import pytest
from playwright.async_api import Error as PlaywrightError

from hoyo_roster import browser_runtime as runtime, platform_io as io
from hoyo_roster.errors import (
    AuthenticationRequired,
    BrowserProfileInvalid,
    BrowserUnavailable,
    RiskControl,
    WrongAccount,
)
from hoyo_roster.storage.common import atomic_write_metadata, secure_private_directory


@pytest.fixture
def profile(tmp_path):
    return secure_private_directory(tmp_path / "synthetic-profile")


class Chromium:
    executable_path = __file__

    def __init__(self, failures=None):
        self.calls = []
        self.failures = failures or {}

    async def launch_persistent_context(self, **kwargs):
        self.calls.append(kwargs)
        failure = self.failures.get(kwargs["channel"])
        if failure:
            raise failure
        return SimpleNamespace(channel=kwargs["channel"])


def launch(engine, profile, *, headed=True):
    return asyncio.run(
        runtime.launch_profile(SimpleNamespace(chromium=engine), profile, headed=headed)
    )


@pytest.mark.parametrize(
    "platform,channel", (("win32", "msedge"), ("darwin", "chrome"), ("linux", "chromium"))
)
def test_platform_preference(monkeypatch, platform, channel):
    monkeypatch.setattr(runtime, "sys", SimpleNamespace(platform=platform))
    assert runtime.preferred_channel() == channel


@pytest.mark.parametrize("preferred", ("msedge", "chrome", "chromium"))
def test_selection_pinned_across_modes_and_preference_changes(profile, monkeypatch, preferred):
    monkeypatch.setattr(runtime, "preferred_channel", lambda: preferred)
    engine = Chromium()
    launch(engine, profile)
    monkeypatch.setattr(runtime, "preferred_channel", lambda: "chrome")
    launch(engine, profile, headed=False)
    assert [call["channel"] for call in engine.calls] == [preferred, preferred]
    assert [call["headless"] for call in engine.calls] == [False, True]
    assert all(
        call["user_data_dir"] == runtime.chromium_profile_path(profile / preferred)
        for call in engine.calls
    )
    assert io.private(profile / runtime.CHOICE_FILE, 0o600)
    assert io.private(profile / preferred, 0o700)


@pytest.mark.parametrize("preferred", ("msedge", "chrome"))
def test_missing_or_failed_preferred_browser_uses_separate_fallback(
    profile, monkeypatch, preferred, capsys
):
    monkeypatch.setattr(runtime, "preferred_channel", lambda: preferred)
    engine = Chromium({preferred: PlaywrightError("synthetic private startup detail")})
    assert launch(engine, profile).channel == "chromium"
    assert [call["channel"] for call in engine.calls] == [preferred, "chromium"]
    assert len({call["user_data_dir"] for call in engine.calls}) == 2
    assert runtime.saved_channel(profile) == "chromium"
    output = capsys.readouterr().out
    assert "BROWSER_FALLBACK=CHROMIUM" in output and "private startup detail" not in output
    launch(engine, profile, headed=False)
    assert engine.calls[-1]["channel"] == "chromium"


def test_system_browser_does_not_require_downloaded_chromium(profile, monkeypatch):
    monkeypatch.setattr(runtime, "preferred_channel", lambda: "msedge")
    engine = Chromium()
    engine.executable_path = str(profile / "not-installed")
    assert launch(engine, profile).channel == "msedge"


@pytest.mark.parametrize("preferred", ("msedge", "chromium"))
def test_missing_fallback_is_pinned_and_can_resume_after_install(profile, monkeypatch, preferred):
    monkeypatch.setattr(runtime, "preferred_channel", lambda: preferred)
    engine = Chromium({"msedge": PlaywrightError("synthetic missing Edge")})
    engine.executable_path = str(profile / "not-installed")
    with pytest.raises(BrowserUnavailable, match="playwright install chromium"):
        launch(engine, profile)
    assert runtime.saved_channel(profile) == "chromium"
    engine.executable_path = __file__
    assert launch(engine, profile).channel == "chromium"


def test_selected_system_browser_failure_does_not_fallback(profile, monkeypatch):
    monkeypatch.setattr(runtime, "preferred_channel", lambda: "msedge")
    engine = Chromium()
    launch(engine, profile)
    engine.failures["msedge"] = PlaywrightError("synthetic update/policy failure")
    with pytest.raises(BrowserUnavailable):
        launch(engine, profile, headed=False)
    assert [call["channel"] for call in engine.calls] == ["msedge", "msedge"]
    assert not (profile / "chromium").exists()


@pytest.mark.parametrize(
    "failure", (asyncio.CancelledError(), RuntimeError("synthetic unexpected failure"))
)
def test_cancellation_or_unexpected_startup_failure_does_not_fallback(
    profile, monkeypatch, failure
):
    monkeypatch.setattr(runtime, "preferred_channel", lambda: "msedge")
    engine = Chromium({"msedge": failure})
    with pytest.raises(type(failure)):
        launch(engine, profile)
    assert len(engine.calls) == 1 and runtime.saved_channel(profile) == "msedge"


def test_unmarked_existing_profile_is_retained_without_launch(profile):
    private_file = profile / "synthetic-existing-state"
    atomic_write_metadata(private_file, {"synthetic": True})
    before = private_file.read_bytes()
    engine = Chromium()
    with pytest.raises(BrowserProfileInvalid):
        launch(engine, profile)
    assert not engine.calls and private_file.read_bytes() == before
    assert not (profile / runtime.CHOICE_FILE).exists()


@pytest.mark.parametrize(
    "value",
    (
        {"version": True, "channel": "msedge"},
        {"version": 1, "channel": "safari"},
        {"version": 1, "channel": "../chrome"},
        {"version": 1, "channel": "chrome", "extra": True},
    ),
)
def test_invalid_selection_does_not_launch(profile, value):
    atomic_write_metadata(profile / runtime.CHOICE_FILE, value)
    engine = Chromium()
    with pytest.raises(BrowserProfileInvalid):
        launch(engine, profile)
    assert not engine.calls


def test_missing_selected_directory_does_not_recreate_empty_session(profile):
    atomic_write_metadata(profile / runtime.CHOICE_FILE, {"version": 1, "channel": "msedge"})
    engine = Chromium()
    with pytest.raises(BrowserProfileInvalid):
        launch(engine, profile)
    assert not engine.calls and not (profile / "msedge").exists()


def test_metadata_failure_prevents_browser_start(profile, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("synthetic metadata failure")

    monkeypatch.setattr(runtime, "atomic_write_metadata", fail)
    engine = Chromium()
    with pytest.raises(BrowserProfileInvalid):
        launch(engine, profile)
    assert not engine.calls


@pytest.mark.parametrize("channel", runtime.CHANNELS)
def test_nested_singleton_detected_in_shared_profile(profile, channel):
    directory = secure_private_directory(profile / channel)
    (directory / "SingletonLock").touch()
    assert runtime.profile_in_use(profile)


def test_shared_profile_pins_selected_browser_without_moving(profile, monkeypatch):
    monkeypatch.setattr(runtime, "preferred_channel", lambda: "msedge")
    engine = Chromium()
    launch(engine, profile)
    monkeypatch.setattr(runtime, "preferred_channel", lambda: "chrome")
    assert launch(engine, profile, headed=False).channel == "msedge"
    assert engine.calls[-1]["user_data_dir"] == runtime.chromium_profile_path(profile / "msedge")


def test_duplicate_metadata_keys_rejected(profile):
    marker = profile / runtime.CHOICE_FILE
    descriptor = io.open_private(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    with os.fdopen(descriptor, "w") as stream:
        stream.write('{"version":1,"channel":"msedge","channel":"chromium"}')
    with pytest.raises(BrowserProfileInvalid):
        launch(Chromium(), profile)


@pytest.mark.parametrize("game", ("genshin", "zzz"))
@pytest.mark.parametrize(
    "error", (WrongAccount, AuthenticationRequired, RiskControl, PlaywrightError)
)
def test_business_failure_after_launch_closes_without_fallback(
    profile, tmp_path, monkeypatch, game, error
):
    from hoyo_roster import auth as browser

    monkeypatch.setattr(runtime, "preferred_channel", lambda: "msedge")
    engine = Chromium()
    events = []
    context = SimpleNamespace(pages=[])

    async def close():
        events.append("close")

    context.close = close

    async def start_context(**kwargs):
        engine.calls.append(kwargs)
        return context

    engine.launch_persistent_context = start_context

    class Runtime:
        chromium = engine

        async def start(self):
            return self

        async def stop(self):
            events.append("stop")

    monkeypatch.setattr(browser, "async_playwright", Runtime)

    async def use():
        async with REAL_ACCOUNT_BROWSER(profile, headed=True):
            raise error("synthetic business failure")

    with pytest.raises(error):
        asyncio.run(use())
    assert events == ["close", "stop"]
    assert len(engine.calls) == 1 and runtime.saved_channel(profile) == "msedge"


@pytest.mark.parametrize("channel", runtime.CHANNELS)
def test_logout_retains_profile_with_nested_singleton(tmp_path, source, channel):
    from hoyo_roster.auth import AuthService
    from hoyo_roster.user_workspace import WorkspaceError

    service = AuthService(tmp_path / "auth", headed=False, timeout=1)
    directory = secure_private_directory(service.profile / channel)
    (directory / "SingletonLock").touch()

    async def run():
        async with service:
            with pytest.raises(WorkspaceError, match="BROWSER_PROFILE_IN_USE_OR_STALE"):
                service.logout()

    asyncio.run(run())
    assert (directory / "SingletonLock").exists()


@pytest.mark.parametrize(
    "error,hint",
    (
        (BrowserUnavailable, "python -m playwright install chromium"),
        (BrowserProfileInvalid, "--auth-root"),
    ),
)
def test_cli_browser_error_has_actionable_private_hint(monkeypatch, capsys, error, hint):
    from hoyo_roster import cli

    async def fail(args):
        raise error("synthetic private details must not be printed")

    monkeypatch.setattr(cli, "run", fail)
    assert cli.main([]) == 1
    output = capsys.readouterr().out
    assert f"ERROR_CODE={error.code}" in output and hint in output
    assert "private details" not in output and "--login-method" not in output
