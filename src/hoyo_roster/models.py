"""Exact Binding probe results."""

from dataclasses import dataclass, field
from enum import StrEnum


class LoginProbeStatus(StrEnum):
    AUTH_REQUIRED = "AUTH_REQUIRED"
    WRONG_ACCOUNT = "WRONG_ACCOUNT"
    RATE_LIMITED = "RATE_LIMITED"
    RISK_CONTROL = "RISK_CONTROL"
    NETWORK_ERROR = "NETWORK_ERROR"
    BINDING_FAILED = "BINDING_FAILED"
    PASS = "PASS"


@dataclass(frozen=True, slots=True)
class LoginProbeResult:
    status: LoginProbeStatus
    identity: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.status is LoginProbeStatus.PASS and (not self.identity):
            raise ValueError("a successful login probe requires an identity")
        if self.status is not LoginProbeStatus.PASS and self.identity is not None:
            raise ValueError("only a successful login probe may carry an identity")


@dataclass(frozen=True, slots=True, repr=False)
class OwnedRole:
    game: str
    uid: str = field(repr=False)
    region: str
