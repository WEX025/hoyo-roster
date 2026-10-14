"""Cancellation cannot strand a terminal reader in asyncio's exit-time executor."""

import asyncio
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from time import monotonic

import pytest

from hoyo_roster.terminal import read_terminal


def test_cancelled_reader_discards_late_result(monkeypatch):
    started, release = threading.Event(), threading.Event()

    def input_wait(prompt):
        started.set()
        release.wait(5)
        return "phone"

    monkeypatch.setattr("builtins.input", input_wait)

    async def run():
        task = asyncio.create_task(read_terminal("synthetic selection"))
        while not started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await asyncio.sleep(0.02)

    asyncio.run(run())


@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGINT subprocess acceptance")
def test_sigint_exits_without_another_line_of_input():
    code = """import asyncio, sys
from hoyo_roster.terminal import read_terminal
try:
    asyncio.run(read_terminal('BLOCKED_INPUT:'))
except KeyboardInterrupt:
    sys.exit(130)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stdout.read(len(b"BLOCKED_INPUT:")) == b"BLOCKED_INPUT:"
        process.send_signal(signal.SIGINT)
        assert process.wait(timeout=5) == 130
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX terminal echo restoration")
def test_hidden_input_cancel_restores_terminal_echo():
    import pty
    import termios
    import time

    master, slave = pty.openpty()
    original = termios.tcgetattr(slave)
    code = """import asyncio, sys
from hoyo_roster.terminal import read_terminal
try:
    asyncio.run(read_terminal('HIDDEN_INPUT:', hidden=True))
except KeyboardInterrupt:
    sys.exit(130)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stdout.read(len(b"HIDDEN_INPUT:")) == b"HIDDEN_INPUT:"
        deadline = monotonic() + 2
        while termios.tcgetattr(slave)[3] & termios.ECHO and monotonic() < deadline:
            time.sleep(0.01)
        assert not termios.tcgetattr(slave)[3] & termios.ECHO
        process.send_signal(signal.SIGINT)
        assert process.wait(timeout=5) == 130
        assert termios.tcgetattr(slave) == original
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
        os.close(master)
        os.close(slave)


@pytest.mark.skipif(
    os.name == "nt" or os.environ.get("HOYO_NATIVE_LOGIN_SMOKE") != "1",
    reason="opt-in POSIX native browser cancellation smoke",
)
def test_process_group_ctrl_c_closes_browser_without_second_input(tmp_path):
    code = """import asyncio, sys
from pathlib import Path
from hoyo_roster.cli import CONTRACT_ROOT
from hoyo_roster.games.genshin.browser import persistent_browser
from hoyo_roster.login_ui import choose_method
from hoyo_roster.progress import OPTIONS, LoginOptions
from hoyo_roster.sync import operation_browser
sys.stdin.isatty = lambda: True
async def run():
    token = OPTIONS.set(LoginOptions())
    try:
        async with operation_browser(persistent_browser(
            repo_root=CONTRACT_ROOT, browser_profile=Path(sys.argv[1]), headed=False
        )) as session:
            await session.context.route('**/*', lambda route: route.abort())
            await choose_method()
    finally:
        OPTIONS.reset(token)
try:
    asyncio.run(run())
except KeyboardInterrupt:
    sys.exit(130)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path / "synthetic-profile")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    captured = b""
    try:
        # Read only this synthetic process's fixed login notice; no official page.
        deadline = monotonic() + 20
        while not captured.endswith("取消]：".encode()) and monotonic() < deadline:
            import select

            if select.select([process.stdout], [], [], 0.1)[0]:
                value = os.read(process.stdout.fileno(), 4096)
                if not value:
                    break
                captured += value
        assert captured.endswith("取消]：".encode())
        os.killpg(process.pid, signal.SIGINT)
        out, err = process.communicate(timeout=10)
        captured += out
        assert process.returncode == 130
        assert b"BROWSER_CLOSE=CLOSED" in captured and b"BROWSER_CLOSE=FAILED" not in captured
        assert b"Traceback" not in err
        assert not any(Path(tmp_path).rglob("SingletonLock"))
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if not stream.closed:
                stream.close()
