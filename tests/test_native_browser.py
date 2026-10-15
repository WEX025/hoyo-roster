"""Native test setup must not discover browser availability by repeated launches."""

from types import SimpleNamespace

import pytest

from tools import native_browser


@pytest.mark.parametrize("preferred", ("chrome", "msedge", "chromium"))
def test_auto_uses_installed_platform_browser_without_trying_another(monkeypatch, preferred):
    checked = []
    monkeypatch.setattr(native_browser, "preferred_channel", lambda: preferred)
    monkeypatch.setattr(
        native_browser, "installed", lambda channel: checked.append(channel) or True
    )
    assert native_browser.choose_channel("auto") == preferred
    assert checked == [preferred]


def test_auto_missing_system_browser_checks_bundled_executable_without_launch(monkeypatch):
    checked = []
    monkeypatch.setattr(native_browser, "preferred_channel", lambda: "chrome")
    monkeypatch.setattr(
        native_browser,
        "installed",
        lambda channel: checked.append(channel) or channel == "chromium",
    )
    assert native_browser.choose_channel("auto") == "chromium"
    assert checked == ["chrome", "chromium"]


@pytest.mark.parametrize("channel", ("chrome", "msedge", "chromium"))
def test_explicit_missing_browser_stops_without_fallback(monkeypatch, channel):
    checked = []
    monkeypatch.setattr(native_browser, "installed", lambda value: checked.append(value) or False)
    with pytest.raises(native_browser.BrowserNotInstalled, match="NATIVE_BROWSER_NOT_INSTALLED"):
        native_browser.choose_channel(channel)
    assert checked == [channel]


def test_missing_browser_never_starts_native_suite(monkeypatch, capsys):
    monkeypatch.setattr(native_browser, "installed", lambda channel: False)
    monkeypatch.setattr(
        native_browser.subprocess, "run", lambda *a, **kw: pytest.fail("must not launch tests")
    )
    assert native_browser.main(["--channel", "chrome"]) == 2
    assert "NATIVE_CHECK=BLOCKED" in capsys.readouterr().out


def test_check_only_checks_installation_without_launch(monkeypatch, capsys):
    monkeypatch.setattr(native_browser, "installed", lambda channel: True)
    monkeypatch.setattr(
        native_browser.subprocess, "run", lambda *a, **kw: pytest.fail("check-only cannot launch")
    )
    assert native_browser.main(["--channel", "chrome", "--check-only"]) == 0
    assert "BROWSER_LAUNCH=NOT_RUN" in capsys.readouterr().out


@pytest.mark.parametrize("suite", ("login", "profile"))
def test_failed_suite_is_not_retried_or_switched(monkeypatch, suite):
    calls = []
    monkeypatch.setattr(native_browser, "installed", lambda channel: True)

    def failed(command, *, cwd, env):
        calls.append((command, env))
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(native_browser.subprocess, "run", failed)
    assert native_browser.main(["--channel", "chrome", "--suite", suite]) == 7
    assert len(calls) == 1
    command, env = calls[0]
    assert env["HOYO_TEST_BROWSER_CHANNEL"] == "chrome"
    if suite == "login":
        assert "-x" in command
    else:
        assert command[-2:] == ["--channel", "chrome"]


def test_native_profile_is_pinned_and_existing_profile_is_preserved(tmp_path):
    from hoyo_roster.storage.common import load_metadata, tree_digest

    profile = native_browser.prepare_profile(tmp_path / "synthetic", "chrome")
    assert load_metadata(profile / "browser-choice.json", private=True) == {
        "version": 1,
        "channel": "chrome",
    }
    before = tree_digest(profile)
    with pytest.raises(ValueError, match="NATIVE_TEST_PROFILE_NOT_EMPTY"):
        native_browser.prepare_profile(profile, "chromium")
    assert tree_digest(profile) == before
