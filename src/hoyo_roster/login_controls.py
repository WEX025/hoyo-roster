"""Bounded readiness waits for ordinary official login controls, without DOM logging."""

from time import monotonic

from .progress import emit


class ControlReadiness:
    def __init__(self, stage, *, grace=15.0):
        self.stage = stage
        self.grace = grace
        self.since = None
        self.reason = None

    def waiting(self, reason):
        now = monotonic()
        if self.since is None:
            self.since = now
        if self.reason != reason:
            self.reason = reason
            emit("WAITING_FOR_LOGIN_CONTROLS")
            print(f"CONTROL_STAGE={self.stage}\nCONTROL_REASON={reason}", flush=True)
        return now - self.since >= self.grace

    def ready(self):
        self.since = self.reason = None
