"""Selection only when needed, cancellation, absent official entry and privacy."""

import asyncio
import pytest
from hoyo_roster.errors import AuthenticationRequired, LoginCancelled, LoginMethodUnavailable
from hoyo_roster.login_ui import choose_method, confirm_method_entry
from hoyo_roster.progress import OPTIONS, LoginOptions
from tests.test_login_flow import coordinate, provider_for, binding_payload, UID_A, Page
from hoyo_roster.user_workspace import empty_workspace


@pytest.mark.parametrize("method", ("qr", "phone"))
def test_noninteractive_explicit_choice_needs_no_input(monkeypatch, method):
    monkeypatch.setattr("builtins.input", lambda *args: pytest.fail("No terminal credential input"))
    token = OPTIONS.set(LoginOptions(method, True))
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
    assert not page.urls and not context.pages and context.request.count == 1
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
    assert not page.urls and not context.pages


@pytest.mark.parametrize("method", ("qr", "phone"))
def test_absent_method_entry_is_explicit_not_fake_success(method, capsys):
    class AbsentPage(Page):
        async def is_visible(self):
            return False

    token = OPTIONS.set(LoginOptions(method, True, timeout=0.001))
    try:
        with pytest.raises(LoginMethodUnavailable):
            asyncio.run(confirm_method_entry(AbsentPage(), method))
    finally:
        OPTIONS.reset(token)
    output = capsys.readouterr().out
    assert "METHOD_UNAVAILABLE" in output and "LOGIN_ENTRY=OBSERVED" not in output
