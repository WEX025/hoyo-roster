"""The shared persistent gate spaces concurrent reservations and reports queue time."""

import asyncio
import time

from hoyo_roster.config import Settings
from hoyo_roster.request_gate import RequestGate
from hoyo_roster import platform_io as io


def test_concurrent_reservations_keep_two_second_spacing_and_queue_metrics(tmp_path):
    root = tmp_path / "runtime"
    io.mkdir_private(root)

    async def run():
        gate = RequestGate(Settings(external_root=root))
        observed = []

        async def reserve():
            await gate.reserve(requested_min_interval=2)
            observed.append((time.monotonic(), dict(gate.last_reservation)))

        await asyncio.gather(reserve(), reserve(), reserve())
        assert all(b[0] - a[0] >= 1.99 for a, b in zip(observed, observed[1:]))
        assert observed[-1][1]["queue_wait_ms"] >= 1900
        assert observed[-1][1]["total_ms"] >= observed[-1][1]["queue_wait_ms"]

    asyncio.run(run())


def test_cross_game_page_reservations_and_conservative_restart(tmp_path, monkeypatch):
    clock = [500.0]
    monkeypatch.setattr("hoyo_roster.request_gate.time.time", lambda: clock[0])

    async def advance(wait):
        clock[0] += wait

    monkeypatch.setattr("hoyo_roster.request_gate.asyncio.sleep", advance)
    root = tmp_path / "runtime"
    io.mkdir_private(root)

    async def run():
        gate = RequestGate(Settings(external_root=root))
        gate.cross_game_interval_ms = 1500
        for game, scheduled in [
            ("genshin", 500),
            ("zzz", 501.5),
            ("zzz", 503.5),
            (None, 505.5),
            ("genshin", 507.5),
            ("zzz", 509),
        ]:
            assert await gate.reserve(requested_min_interval=2, page_game=game) == scheduled
        fresh = RequestGate(Settings(external_root=root))
        fresh.cross_game_interval_ms = 1000
        assert await fresh.reserve(requested_min_interval=2, page_game="genshin") == 511

    asyncio.run(run())
