"""Native IOCP wakeups must work without a localhost network connection."""

import asyncio
import socket
import threading

import pytest

from hoyo_roster import platform_io as io

pytestmark = pytest.mark.skipif(not io.WINDOWS, reason="native Windows IOCP")


def test_pipe_loop_cross_thread_wakeup_and_saturation(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("local task scheduling must not create TCP sockets")

    monkeypatch.setattr(socket, "socketpair", forbidden)
    count = 20000

    async def exercise():
        loop = asyncio.get_running_loop()
        completed = loop.create_future()
        seen = []

        def receive(value):
            seen.append(value)
            if len(seen) == count:
                completed.set_result(None)

        def notify():
            for value in range(count):
                loop.call_soon_threadsafe(receive, value)

        producer = threading.Thread(target=notify)
        producer.start()
        # Let the writer fill the wakeup buffer before processing callbacks.
        await asyncio.to_thread(producer.join, 5)
        assert not producer.is_alive()
        await asyncio.wait_for(completed, 5)
        assert seen == list(range(count))
        assert await asyncio.to_thread(lambda: 7) == 7
        return loop

    loop = io.run_async(exercise())
    assert loop.is_closed()
    assert loop._ssock is None and loop._csock is None


def test_pipe_loop_cancels_tasks_and_releases_handles():
    finalized = []

    async def pending():
        try:
            await asyncio.Future()
        finally:
            finalized.append(True)

    async def exercise():
        asyncio.create_task(pending())
        await asyncio.sleep(0)

    for _ in range(3):
        io.run_async(exercise())
    assert finalized == [True] * 3


def test_pipe_loop_runner_handles_sigint():
    import signal

    finalized = []

    async def exercise():
        try:
            asyncio.get_running_loop().call_soon(signal.raise_signal, signal.SIGINT)
            await asyncio.sleep(5)
        finally:
            finalized.append(True)

    with pytest.raises(KeyboardInterrupt):
        io.run_async(exercise())
    assert finalized == [True]


def test_unreadable_prior_sid_directory_is_rejected(monkeypatch, tmp_path):
    import pywintypes

    def denied(_path):
        raise pywintypes.error(5, "GetNamedSecurityInfo", "synthetic access denied")

    monkeypatch.setattr(io.backend, "_descriptor", denied)
    assert not io.private(tmp_path)


def test_asyncio_private_contract_change_stops_before_pipe_creation(monkeypatch):
    from asyncio import proactor_events
    from hoyo_roster.errors import WorkspaceError

    def changed_constructor(self):
        raise AssertionError("unreviewed constructor must never run")

    monkeypatch.setattr(proactor_events.BaseProactorEventLoop, "__init__", changed_constructor)
    with pytest.raises(WorkspaceError, match="WINDOWS_ASYNCIO_UNSUPPORTED_RUNTIME"):
        io.backend.new_event_loop()


def test_loop_initialization_failure_closes_unstarted_coroutine(monkeypatch):
    import inspect
    from hoyo_roster.errors import WorkspaceError

    def unsupported():
        raise WorkspaceError("WINDOWS_ASYNCIO_UNSUPPORTED_RUNTIME")

    async def pending():
        pytest.fail("unsupported runtime cannot start business work")

    monkeypatch.setattr(io.backend, "new_event_loop", unsupported)
    coroutine = pending()
    with pytest.raises(WorkspaceError, match="WINDOWS_ASYNCIO_UNSUPPORTED_RUNTIME"):
        io.run_async(coroutine)
    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED


def test_unsupported_runtime_cli_points_to_runtime_not_storage_recovery(monkeypatch, capsys):
    from hoyo_roster import cli
    from hoyo_roster.errors import WorkspaceError

    def unsupported():
        raise WorkspaceError("WINDOWS_ASYNCIO_UNSUPPORTED_RUNTIME")

    monkeypatch.setattr(io.backend, "new_event_loop", unsupported)
    assert cli.main(["--game", "genshin", "--uid", "123456789", "--operation", "inspect"]) == 1
    output = capsys.readouterr().out
    assert "ERROR_CODE=WINDOWS_ASYNCIO_UNSUPPORTED_RUNTIME" in output
    assert "CPython 3.12.14" in output
    assert "执行 --operation inspect" not in output
