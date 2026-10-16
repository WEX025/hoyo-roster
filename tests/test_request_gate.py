"""The shared persistent gate spaces concurrent reservations and reports queue time."""

import asyncio
import time

from hoyo_roster.config import Settings
from hoyo_roster.request_gate import RequestGate


def test_concurrent_reservations_keep_two_second_spacing_and_queue_metrics(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)

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
