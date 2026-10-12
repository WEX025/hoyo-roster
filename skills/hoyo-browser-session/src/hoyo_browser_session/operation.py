"""Typed contract for the fixed in-process Owner operation registry."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .browser import BrowserOwnerSession
from .config import Settings
from .models import ExecutorResult, Job, JobParameters
from .request_gate import RequestGate

OperationKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class OperationIdentity:
    game: str
    operation: str

    def __post_init__(self) -> None:
        if not self.game or not self.operation:
            raise ValueError("operation identity values must be non-empty")

    @property
    def key(self) -> OperationKey:
        return self.game, self.operation


class BrowserAuthority(StrEnum):
    ROUTINE = "ROUTINE"
    LOGIN = "LOGIN"


class OperationAdapter(Protocol):
    """One statically registered operation owned by executable code."""

    @property
    def identity(self) -> OperationIdentity: ...

    @property
    def browser_authority(self) -> BrowserAuthority: ...

    @property
    def current_contract_version(self) -> int: ...

    def validate_parameters(self, parameters: JobParameters) -> None: ...

    async def execute(
        self,
        job: Job,
        settings: Settings,
        browser: BrowserOwnerSession,
        request_gate: RequestGate,
    ) -> ExecutorResult: ...

    def legacy_contract_version(self, result_schema_version: int) -> int: ...

    def validate_live_result(self, result: ExecutorResult) -> None: ...

    def validate_persisted_result(
        self,
        result: ExecutorResult,
        contract_version: int,
    ) -> None: ...
