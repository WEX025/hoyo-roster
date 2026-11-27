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
def test_parallel_recovery_quiesces_peers_and_rechecks_pinned_identities(tmp_path, changed, capsys):
    port = ConcurrentPort()
    port.expire_at.add(("zzz", "page:detail", 1))
    port.login_changes_account = changed
    assert cli.main(arguments(tmp_path, "--parallel-games"), client=port.client()) == int(changed)
    assert sum(c["method"] == "login.confirm" for c in port.calls) == 1
    if changed:
        assert not snapshots(tmp_path)
    else:
        assert len(snapshots(tmp_path)) == 2
        output = capsys.readouterr().out.splitlines()
        assert "COLLECTION_MODE=SERIAL_RECOVERY" not in output
        for game in ("genshin", "zzz"):
            states = [
                line.split(":")[1] for line in output if line.startswith(f"GAME_STATE={game}:")
            ]
            assert states == [
                "CHECKING_IDENTITY",
                "COLLECTING",
                "WAITING_LOGIN",
                "CHECKING_IDENTITY",
                "COLLECTING",
                "COMPLETED",
            ]
        # Both interrupted, uncommitted game observations are read again.
        assert port.counts["genshin", "page:roster"] >= 2
        assert port.counts["zzz", "page:roster"] >= 3


def test_parallel_recovery_does_not_reuse_confirmation_for_another_expiry(tmp_path):
    port = ConcurrentPort()
    for n in [1, 2]:
        port.expire_at.add(("zzz", "page:detail", n))
    assert cli.main(arguments(tmp_path, "--parallel-games"), client=port.client()) == 1
    assert sum(c["method"] == "login.confirm" for c in port.calls) == 1


def test_local_recheck_leaves_active_peer_running(tmp_path):
    class LocalExpiry(ConcurrentPort):
        def __init__(self):
            super().__init__()
            self.peer_reading = asyncio.Event()
            self.probe_finished = asyncio.Event()
            self.peer_finished = False

        async def exchange(self, message):
            params = message["params"]
            if (
                params.get("game") == "genshin"
                and params.get("source") == "page:roster"
                and not self.peer_finished
            ):
                self.peer_reading.set()
                await asyncio.wait_for(self.probe_finished.wait(), 2)
                self.peer_finished = True
            if params.get("game") == "zzz" and params.get("source") == "page:detail":
                await asyncio.wait_for(self.peer_reading.wait(), 2)
            result = await super().exchange(message)
            if params.get("game") == "zzz" and params.get("recheck"):
                self.probe_finished.set()
            return result

    port = LocalExpiry()
    port.fail_at["zzz", "page:detail", 1] = "AUTHENTICATION_REQUIRED"
    assert cli.main(arguments(tmp_path), client=port.client()) == 0
    assert port.peer_finished and len(snapshots(tmp_path)) == 2
    assert port.counts["genshin", "page:roster"] == 2
    assert port.counts["zzz", "page:roster"] == 3
    assert not any(c["method"].startswith("login.") for c in port.calls)


def test_healthy_identity_with_persistent_denial_only_fails_that_game(tmp_path):
    port = ConcurrentPort()
    for n in (1, 2):
        port.fail_at["zzz", "page:detail", n] = "AUTHENTICATION_REQUIRED"
    assert cli.main(arguments(tmp_path), client=port.client()) == 1
    assert len(snapshots(tmp_path)) == 1
    assert port.counts["genshin", "page:detail"] == 2
    assert port.counts["zzz", "page:detail"] == 2
    assert not any(c["method"].startswith("login.") for c in port.calls)


def test_initial_login_precedes_both_double_passes_and_preserves_parallelism(tmp_path, capsys):
    port = ConcurrentPort(authenticated=False)
    assert cli.main(arguments(tmp_path), client=port.client()) == 0
    output = capsys.readouterr().out
    assert output.count("COLLECTION_MODE=PARALLEL") == 1
    assert "SERIAL_RECOVERY" not in output
    assert sum(c["method"] == "login.confirm" for c in port.calls) == 1
    confirm = next(i for i, c in enumerate(port.calls) if c["method"] == "login.confirm")
    reads = [c["params"] for c in port.calls if c["method"] == "source.read"]
    assert all(
        c["params"].get("source", "page:identity") == "page:identity" for c in port.calls[:confirm]
    )
    after = [c["params"] for c in port.calls[confirm:] if c["method"] == "source.read"]
    for game in ("genshin", "zzz"):
        sources = [c["source"] for c in after if c["game"] == game]
        assert sources[0] == "page:identity"
        assert sources.count("page:roster") == 2
    assert sum(c["source"] == "page:roster" for c in reads) == 4


def test_repeated_startup_auth_failure_stops_unverified_game(tmp_path):
    port = ConcurrentPort()
    for n in (1, 3):
        port.expire_at.add(("zzz", "page:identity", n))
    assert cli.main(arguments(tmp_path), client=port.client()) == 1
    assert sum(c["method"] == "login.confirm" for c in port.calls) == 1
    assert port.counts["zzz", "page:roster"] == 0


def test_startup_login_requires_fresh_identity_before_that_game_collects(tmp_path):
    port = ConcurrentPort()
    port.expire_at.add(("zzz", "page:identity", 1))
    port.fail_at["zzz", "page:identity", 3] = "SOURCE_DATA_INVALID"
    assert cli.main(arguments(tmp_path), client=port.client()) == 1
    assert port.counts["zzz", "page:roster"] == 0


@pytest.mark.parametrize(("slow", "fast"), [("genshin", "zzz"), ("zzz", "genshin")])
def test_fast_game_commits_before_peer_finishes_first_identity(tmp_path, slow, fast, capsys):
    class DelayedIdentity(ConcurrentPort):
        async def exchange(self, message):
            params = message["params"]
            if (
                params.get("game") == slow
                and params.get("source") == "page:identity"
                and not self.counts[slow, "page:identity"]
            ):
                async with asyncio.timeout(2):
                    while not snapshots(tmp_path):
                        await asyncio.sleep(0.001)
                assert json.loads(snapshots(tmp_path)[0].read_text())["game"] == fast
                assert self.counts[fast, "page:roster"] == self.counts[fast, "page:detail"] == 2
            return await super().exchange(message)

    assert cli.main(arguments(tmp_path), client=DelayedIdentity().client()) == 0
    events = capsys.readouterr().out.splitlines()
    assert events.index(f"GAME_STATE={fast}:COMPLETED") < events.index(
        f"GAME_STATE={slow}:COLLECTING"
    )
    for game in (fast, slow):
        assert [e.split(":")[1] for e in events if e.startswith(f"GAME_STATE={game}:")] == [
            "CHECKING_IDENTITY",
            "COLLECTING",
            "COMPLETED",
        ]


def test_relogin_restores_independent_scheduling_for_unfinished_games(tmp_path, capsys):
    class DelayedRestart(ConcurrentPort):
        async def exchange(self, message):
            params = message["params"]
            if (
                params.get("game") == "genshin"
                and params.get("source") == "page:roster"
                and not self.generation
            ):
                # This first attempt must be cancelled before the shared login.
                await asyncio.wait_for(asyncio.Event().wait(), 2)
            if (
                self.generation
                and params.get("game") == "zzz"
                and params.get("source") == "page:identity"
                and not params.get("recheck")
                and not snapshots(tmp_path)
            ):
                async with asyncio.timeout(2):
                    while not snapshots(tmp_path):
                        await asyncio.sleep(0.001)
                assert json.loads(snapshots(tmp_path)[0].read_text())["game"] == "genshin"
            return await super().exchange(message)

    port = DelayedRestart()
    port.expire_at.add(("zzz", "page:detail", 1))
    assert cli.main(arguments(tmp_path), client=port.client()) == 0
    events = capsys.readouterr().out.splitlines()
    resumed = events[events.index("PARALLEL_RECOVERY=QUIESCED") :]
    assert resumed.index("GAME_STATE=genshin:COMPLETED") < resumed.index(
        "GAME_STATE=zzz:COLLECTING"
    )
    assert sum(c["method"] == "login.confirm" for c in port.calls) == 1
    assert len(snapshots(tmp_path)) == 2


def test_game_local_startup_failure_leaves_healthy_peer_running(tmp_path, capsys):
    port = ConcurrentPort()
    port.fail_at["zzz", "page:identity", 1] = "SOURCE_DATA_INVALID"
    assert cli.main(arguments(tmp_path), client=port.client()) == 1
    assert port.counts["genshin", "page:roster"] == 2
    assert len(snapshots(tmp_path)) == 1
    events = capsys.readouterr().out.splitlines()
    assert "GAME_STATE=zzz:FAILED" in events
    assert "GAME_STATE=zzz:COLLECTING" not in events
    assert events.count("GAME_STATE=genshin:COMPLETED") == 1


def test_completed_game_is_preserved_when_peer_needs_recovery(tmp_path):
    class LateExpiry(ConcurrentPort):
        async def exchange(self, message):
            if (
                message["method"] == "source.read"
                and message["params"]["game"] == "zzz"
                and message["params"]["source"] == "page:roster"
                and not self.counts["zzz", "page:roster"]
            ):
                while not snapshots(tmp_path):
                    await asyncio.sleep(0.001)
            return await super().exchange(message)

    port = LateExpiry()
    port.expire_at.add(("zzz", "page:roster", 1))
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
