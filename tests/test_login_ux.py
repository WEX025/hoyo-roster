"""Selection only when needed, cancellation, absent official entry and privacy."""

import asyncio
import pytest
from hoyo_roster.errors import AuthenticationRequired, LoginCancelled, LoginTimeout
from hoyo_roster.login_ui import choose_method, confirm_method_entry
from hoyo_roster.progress import OPTIONS, LoginOptions
from tests.test_login_flow import (
    coordinate,
    provider_for,
    binding_payload,
    UID_A,
    Page,
    InvisibleLocator,
)
from hoyo_roster.user_workspace import empty_workspace


@pytest.mark.parametrize("method", ("qr", "phone"))
def test_noninteractive_explicit_choice_needs_no_input(monkeypatch, method):
    monkeypatch.setattr("builtins.input", lambda *args: pytest.fail("No terminal credential input"))
    token = OPTIONS.set(LoginOptions(method, True, allow_login=True))
    try:
        assert asyncio.run(choose_method()) == method
    finally:
        OPTIONS.reset(token)


@pytest.mark.parametrize("answer,method", (("1", "qr"), ("2", "phone")))
def test_interactive_method_choice(monkeypatch, answer, method):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    token = OPTIONS.set(LoginOptions())
    try:
        assert asyncio.run(choose_method()) == method
    finally:
        OPTIONS.reset(token)


def test_noninteractive_missing_choice_stops():
    token = OPTIONS.set(LoginOptions(None, True))
    try:
        with pytest.raises(AuthenticationRequired):
            asyncio.run(choose_method())
    finally:
        OPTIONS.reset(token)


@pytest.mark.parametrize("game", ("genshin", "zzz", "hsr"))
def test_cancel_does_not_open_official_login(tmp_path, source, game, capsys):
    root = tmp_path / "workspace"
    empty_workspace(source, root)
    provider, context, page, gate = provider_for(game, root, [binding_payload(game, retcode=10001)])
    token = OPTIONS.set(LoginOptions("cancel", True))
    try:
        with pytest.raises(LoginCancelled):
            asyncio.run(coordinate(root, provider, context, page, gate))
    finally:
        OPTIONS.reset(token)
    assert not page.urls and all(p.closed and not p.foregrounds for p in context.pages)
    assert context.request.count == 1
    assert UID_A not in capsys.readouterr().out


@pytest.mark.parametrize("game", ("genshin", "zzz", "hsr"))
def test_exact_session_reused_even_with_cancel_choice(tmp_path, source, game):
    root = tmp_path / "workspace"
    empty_workspace(source, root)
    provider, context, page, gate = provider_for(game, root, [binding_payload(game)])
    token = OPTIONS.set(LoginOptions("cancel", True))
    try:
        result = asyncio.run(coordinate(root, provider, context, page, gate))
    finally:
        OPTIONS.reset(token)
    assert result.complete_coverage_success and not result.interaction_required
    assert not page.urls and all(p.closed and not p.foregrounds for p in context.pages)


@pytest.mark.parametrize("method", ("qr", "phone"))
def test_absent_method_entry_is_explicit_not_fake_success(method, capsys):
    class AbsentPage(Page):
        async def is_visible(self):
            return False

    token = OPTIONS.set(LoginOptions(method, True, timeout=0.001))
    try:
        with pytest.raises(LoginTimeout):
            asyncio.run(confirm_method_entry(AbsentPage(), method))
    finally:
        OPTIONS.reset(token)
    output = capsys.readouterr().out
    assert "WAITING_FOR_ENTRY" in output and "LOGIN_ENTRY=OBSERVED" not in output
    assert "METHOD_UNAVAILABLE" not in output


def test_binding_success_precedes_missing_panel(tmp_path, source, capsys):
    root = tmp_path / "workspace"
    empty_workspace(source, root)
    provider, context, page, gate = provider_for(
        "genshin", root, [binding_payload("genshin", retcode=10001), binding_payload("genshin")]
    )
    page.locator = lambda selector: InvisibleLocator()
    token = OPTIONS.set(LoginOptions("qr", True, timeout=0.1, allow_login=True))
    try:
        assert asyncio.run(coordinate(root, provider, context, page, gate)).success
    finally:
        OPTIONS.reset(token)
    output = capsys.readouterr().out
    assert "LOGIN_CONFIRMED" in output and "LOGIN_ENTRY=OBSERVED" not in output


def test_panel_and_identity_wait_share_deadline(tmp_path, source, monkeypatch):
    import hoyo_roster.login as login

    root = tmp_path / "workspace"
    empty_workspace(source, root)
    provider, context, page, gate = provider_for(
        "genshin", root, [binding_payload("genshin", retcode=10001)]
    )
    observed = []

    async def entry(*args, deadline, **kwargs):
        observed.append(deadline)
        await asyncio.sleep(0.03)

    monkeypatch.setattr(login, "confirm_method_entry", entry)
    token = OPTIONS.set(LoginOptions("qr", True, timeout=0.02, allow_login=True))
    try:
        with pytest.raises(LoginTimeout):
            asyncio.run(coordinate(root, provider, context, page, gate))
    finally:
        OPTIONS.reset(token)
    assert len(observed) == 1 and context.request.count == 1
    assert context.pages[0].closed


def test_phone_entry_ready_keeps_manual_wait_until_binding(tmp_path, source, capsys):
    root = tmp_path / "workspace"
    empty_workspace(source, root)
    provider, context, page, gate = provider_for(
        "genshin", root, [binding_payload("genshin", retcode=10001)]
    )
    token = OPTIONS.set(LoginOptions("phone", True, timeout=0.02, allow_login=True))
    try:
        with pytest.raises(LoginTimeout):
            asyncio.run(coordinate(root, provider, context, page, gate))
    finally:
        OPTIONS.reset(token)
    output = capsys.readouterr().out
    assert "PROGRESS=WAITING_FOR_PHONE_AND_TERMS" in output
    assert "PROGRESS=VERIFYING_IDENTITY" not in output
    assert "PROGRESS=SMS_REQUESTING" not in output


@pytest.mark.parametrize(
    "url",
    (
        "https://act.mihoyo.com.evil.invalid/ys/event/calculator/",
        "https://act.mihoyo.com:444/ys/event/calculator/",
        "http://act.mihoyo.com/ys/event/calculator/",
        "https://user:pass@act.mihoyo.com/ys/event/calculator/",
        "https://act.mihoyo.com/other/",
    ),
)
def test_panel_click_requires_exact_official_origin(url):
    from types import SimpleNamespace
    from hoyo_roster.login_ui import panel_page

    assert not panel_page(SimpleNamespace(url=url), "genshin")


@pytest.mark.parametrize(
    "url",
    (
        "https://user.mihoyo.com.evil.invalid/login-platform/index.html",
        "http://user.mihoyo.com/login-platform/index.html",
        "https://user.mihoyo.com:444/login-platform/index.html",
        "https://user:pass@user.mihoyo.com/login-platform/index.html",
        "https://user.mihoyo.com/other/",
    ),
)
def test_mode_click_requires_exact_sdk_origin(url):
    from types import SimpleNamespace
    from hoyo_roster.login_ui import sdk_frame

    assert not sdk_frame(SimpleNamespace(url=url))
