"""Real three-game CLI chains preserve candidates on storage commit failures."""

import os
import stat
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from hoyo_roster import cli, user_workspace as uw
from hoyo_roster.storage.common import load_metadata, tree_digest
from tests.test_direct_entrypoint import BusinessPage
from tests.test_game_chains import OfficialContext
from tests.test_login_flow import Context, UID_A, binding_payload


@pytest.mark.parametrize("game", uw.GAMES)
@pytest.mark.parametrize("after_replace", (False, True))
def test_sync_rejects_storage_failure_without_deleting_candidate(
    tmp_path, monkeypatch, capsys, game, after_replace
):
    import importlib

    domain = importlib.import_module(f"hoyo_roster.games.{game}.cli")
    browser = importlib.import_module(f"hoyo_roster.games.{game}.browser")
    context = Context([binding_payload(game)]) if game == "genshin" else OfficialContext(game)
    if game != "genshin":
        context.request.login_expired = False
    if game == "hsr":
        monkeypatch.setattr(browser, "_profile_has_active_browser_root", lambda profile: False)

    @asynccontextmanager
    async def simulated_browser(**kwargs):
        page = BusinessPage(UID_A) if game == "genshin" else await context.new_page()
        session = browser.BrowserSession(None, context, page, kwargs["headed"])
        if game == "zzz":
            page.on("request", lambda request: browser._capture_rpc_headers(session, request))
        try:
            yield session
        finally:
            if game == "hsr" and session.business_guard_installed:
                await browser.remove_business_guard(session)

    from hoyo_roster.request_gate import RequestGate

    async def reserve(self, **kwargs):
        return 0

    monkeypatch.setattr(domain, "persistent_browser", simulated_browser)
    monkeypatch.setattr(RequestGate, "reserve", reserve)
    root = tmp_path / "private"
    store = uw.LocalSnapshotStore(
        cli.CONTRACT_ROOT, uw.direct_configuration(game, UID_A, storage_root=root), game
    )
    original_replace, original_fsync = os.replace, os.fsync
    ready, replaced = False, False

    def replace(src, dst):
        nonlocal ready, replaced
        if (
            Path(dst).name == "candidate-state.json"
            and load_metadata(Path(src))["status"] == "validated"
        ):
            ready = True
        if ready and Path(dst) == store.current_file:
            if not after_replace:
                raise OSError("synthetic commit rejection")
            result = original_replace(src, dst)
            replaced = True
            return result
        return original_replace(src, dst)

    def fsync(fd):
        meta = os.fstat(fd)
        parent = store.root.stat()
        if (
            replaced
            and stat.S_ISDIR(meta.st_mode)
            and (meta.st_dev, meta.st_ino) == (parent.st_dev, parent.st_ino)
        ):
            raise OSError("synthetic commit durability failure")
        return original_fsync(fd)

    with monkeypatch.context() as hooks:
        hooks.setattr(os, "replace", replace)
        hooks.setattr(os, "fsync", fsync)
        assert (
            cli.main(
                [
                    "--game",
                    game,
                    "--uid",
                    UID_A,
                    "--storage-root",
                    str(root),
                    "--allow-login",
                    "--login-method",
                    "phone",
                ]
            )
            == 1
        )
    output = capsys.readouterr().out
    assert "COLLECTING_2" in output and "SAVING" in output
    assert UID_A not in output and str(root) not in output
    candidates = list(store.candidate_base.iterdir())
    assert (
        len(candidates) == 1
        and load_metadata(candidates[0] / "candidate-state.json")["status"] == "validated"
    )
    before = tree_digest(candidates[0])
    journal = load_metadata(store.transaction_file)
    assert all((store.root / journal[key]).exists() for key in ("old", "new"))
    current = store.current()
    assert current.name == journal["new" if after_replace else "old"]
    with store.locked():
        store.recover()
    assert store.current() == current and tree_digest(candidates[0]) == before
