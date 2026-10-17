"""Private JSON-lines worker, invoked only by a client browser host."""

import asyncio
from contextlib import redirect_stdout
import sys

from .client import ClientSession, StdioPort
from .errors import OperationError, WorkspaceError
from .jsonio import json_bytes
from .workflow import run_task
from .platform_io import run_async


def serve(args):
    if sys.stdin.isatty() or sys.stdout.isatty():
        print("ERROR_CODE=CLIENT_BROWSER_REQUIRED")
        return 1
    raw_output = sys.stdout.buffer
    port = StdioPort(sys.stdin.buffer, raw_output)

    def send(value):
        raw_output.write(json_bytes(value, compact=True) + b"\n")
        raw_output.flush()

    class Reports:
        def __init__(self):
            self.pending = ""

        def write(self, text):
            self.pending += text
            while "\n" in self.pending:
                line, self.pending = self.pending.split("\n", 1)
                if line:
                    send({"event": "report", "line": line})
            return len(text)

        def flush(self):
            pass

    code = 1
    with redirect_stdout(Reports()):
        try:
            code = run_async(run_task(args, client=ClientSession(port)))
        except (OperationError, WorkspaceError) as error:
            print("ERROR_CODE=" + (error.code if isinstance(error, OperationError) else str(error)))
        except (KeyboardInterrupt, asyncio.CancelledError):
            code = 130
            print("ERROR_CODE=INTERRUPTED")
        except Exception:
            print("ERROR_CODE=LOCAL_OPERATION_FAILED")
    send({"event": "end", "code": code})
    return code
