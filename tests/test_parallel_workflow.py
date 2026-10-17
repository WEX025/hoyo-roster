"""Concurrent page tasks retain identity, two passes, recovery and protocol correlation."""

import asyncio
import json
import os
import threading

import pytest

from hoyo_roster import cli
from hoyo_roster.client import ClientSession, StdioPort
from hoyo_roster.errors import ClientProtocolError
from tests.test_page_workflow import PagePort, arguments, snapshots


@pytest.fixture(autouse=True)
def no_delays(monkeypatch):
    from hoyo_roster.request_gate import RequestGate

    async def reserve(self, **kwargs):
        return 0

    monkeypatch.setattr(RequestGate, "reserve", reserve)


class ConcurrentPort(PagePort):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.active = set()
        self.peak = 0

    async def exchange(self, message):
        game = message["params"].get("game")
        if message["method"] != "source.read":
            if message["method"].startswith("login."):
                assert not self.active, "Login must wait for all peer readers"
            return await super().exchange(message)
        assert game not in self.active
        self.active.add(game)
        self.peak = max(self.peak, len(self.active))
        try:
            await asyncio.sleep(0.002 if game == "genshin" else 0.001)
            return await super().exchange(message)
        finally:
            self.active.remove(game)


@pytest.mark.parametrize(
    "flags, peak", [((), 2), (("--parallel-games",), 2), (("--no-parallel-games",), 1)]
)
def test_default_parallel_and_explicit_serial_save_two_complete_passes(
    tmp_path, capsys, flags, peak
):
    port = ConcurrentPort()
    assert cli.main(arguments(tmp_path, *flags), client=port.client()) == 0
    assert port.peak == peak
    assert len(snapshots(tmp_path)) == 2
    for game in ["genshin", "zzz"]:
        assert port.counts[game, "page:roster"] == 2
        assert port.counts[game, "page:detail"] == 2
    assert "TASK_STATUS=PASS" in capsys.readouterr().out


@pytest.mark.parametrize("changed", [False, True])
def test_parallel_recovery_quiesces_peers_and_rechecks_pinned_identities(tmp_path, changed):
    port = ConcurrentPort()
    port.fail_at["zzz", "page:detail", 1] = "AUTHENTICATION_REQUIRED"
    port.login_changes_account = changed
    assert cli.main(arguments(tmp_path, "--parallel-games"), client=port.client()) == int(changed)
    assert sum(c["method"] == "login.confirm" for c in port.calls) == 1
    if changed:
        assert not snapshots(tmp_path)
    else:
        assert len(snapshots(tmp_path)) == 2
        # Both interrupted, uncommitted game observations are read again.
        assert port.counts["genshin", "page:roster"] >= 2
        assert port.counts["zzz", "page:roster"] >= 3


def test_parallel_recovery_does_not_reuse_confirmation_for_another_expiry(tmp_path):
    port = ConcurrentPort()
    for n in [1, 2]:
        port.fail_at["zzz", "page:detail", n] = "AUTHENTICATION_REQUIRED"
    assert cli.main(arguments(tmp_path, "--parallel-games"), client=port.client()) == 1
    assert sum(c["method"] == "login.confirm" for c in port.calls) == 1


def test_completed_game_is_preserved_when_peer_needs_recovery(tmp_path):
    class LateExpiry(ConcurrentPort):
        async def exchange(self, message):
            if (
                message["method"] == "source.read"
                and message["params"]["game"] == "zzz"
                and not self.counts["zzz", "page:identity"]
            ):
                while not snapshots(tmp_path):
                    await asyncio.sleep(0.001)
            return await super().exchange(message)

    port = LateExpiry()
    port.fail_at["zzz", "page:identity", 1] = "AUTHENTICATION_REQUIRED"
    assert cli.main(arguments(tmp_path, "--parallel-games"), client=port.client()) == 0
    assert port.counts["genshin", "page:roster"] == 2
    assert len(snapshots(tmp_path)) == 2


@pytest.mark.parametrize("bad_id", [False, True])
def test_private_pipe_routes_out_of_order_replies_and_rejects_unknown_ids(bad_id):
    # Exercise the production private-pipe transport. Windows socket.makefile
    # exposes a WinSock handle, not the CRT descriptor required by os.read.
    incoming_fd, writer_fd = os.pipe()
    reader_fd, outgoing_fd = os.pipe()
    reader, writer = os.fdopen(reader_fd, "rb"), os.fdopen(writer_fd, "wb")
    incoming, outgoing = os.fdopen(incoming_fd, "rb"), os.fdopen(outgoing_fd, "wb")

    def respond():
        requests = [json.loads(incoming.readline()) for _ in range(2)]
        for request in reversed(requests):
            outgoing.write(
                json.dumps(
                    {
                        "version": 1,
                        "id": 99 if bad_id else request["id"],
                        "session": "a" * 32,
                        "result": request["id"],
                        "error": None,
                    }
                ).encode()
                + b"\n"
            )
            outgoing.flush()

    thread = threading.Thread(target=respond, daemon=True)
    thread.start()

    async def run():
        client = ClientSession(StdioPort(reader, writer))
        client.session_ref = "a" * 32
        result = await asyncio.gather(
            client.call("source.read"), client.call("source.read"), return_exceptions=True
        )
        if bad_id:
            assert all(isinstance(x, ClientProtocolError) for x in result)
        else:
            assert result == [1, 2]

    try:
        asyncio.run(run())
        thread.join(timeout=1)
        assert not thread.is_alive()
    finally:
        for stream in [outgoing, writer, incoming, reader]:
            stream.close()
