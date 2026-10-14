"""Cancellable, ephemeral terminal input; never leave an executor blocking exit."""

import asyncio
import getpass
import os
import sys
import threading
import warnings


async def read_terminal(prompt: str, *, hidden: bool = False) -> str:
    if hidden and os.name != "nt" and sys.stdin.isatty():
        return await _hidden_posix(prompt)
    loop = asyncio.get_running_loop()
    result = loop.create_future()

    def complete(value, error):
        if not result.done():
            if error is None:
                result.set_result(value)
            else:
                result.set_exception(error)

    def read():
        value, error = None, None
        try:
            if not hidden:
                value = input(prompt)
            elif sys.stdin.isatty():
                # Refuse getpass's echo fallback instead of exposing a phone number.
                with warnings.catch_warnings():
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    value = getpass.getpass(prompt)
            else:
                print(prompt, flush=True)
                value = sys.stdin.readline()
                if not value:
                    raise EOFError
                value = value.rstrip("\r\n")
        except BaseException as caught:
            error = asyncio.CancelledError() if isinstance(caught, KeyboardInterrupt) else caught
        try:
            loop.call_soon_threadsafe(complete, value, error)
        except RuntimeError:
            pass  # A cancelled CLI has already closed its loop; discard the input.

    # input/getpass cannot be interrupted portably. A daemon reader is deliberately
    # outside asyncio's executor so cancellation can close the browser and exit.
    threading.Thread(target=read, daemon=True, name="hoyo-terminal-input").start()
    return await result


async def _hidden_posix(prompt: str) -> str:
    import termios

    loop = asyncio.get_running_loop()
    descriptor = sys.stdin.fileno()
    original = termios.tcgetattr(descriptor)
    hidden = original.copy()
    hidden[3] &= ~termios.ECHO
    result = loop.create_future()

    def ready():
        if result.done():
            loop.remove_reader(descriptor)
            return
        try:
            value = sys.stdin.readline()
            if not value:
                raise EOFError
            result.set_result(value.rstrip("\r\n"))
        except Exception as error:
            result.set_exception(error)
        finally:
            loop.remove_reader(descriptor)

    termios.tcsetattr(descriptor, termios.TCSADRAIN, hidden)
    try:
        print(prompt, end="", flush=True)
        loop.add_reader(descriptor, ready)
        return await result
    finally:
        loop.remove_reader(descriptor)
        # Also discard an unfinished number on cancellation; restore the caller's tty.
        termios.tcsetattr(descriptor, termios.TCSAFLUSH, original)
        print(flush=True)
