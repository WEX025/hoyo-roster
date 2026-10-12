"""Fixed ZZZ Owner operation and result contract."""

from __future__ import annotations

from dataclasses import dataclass, field

from hoyo_browser_session.browser import BorrowedBrowserSession, BrowserOwnerSession
from hoyo_browser_session.config import REAL_OPERATION, Settings
from hoyo_browser_session.models import ExecutorResult, Job, JobParameters
from hoyo_browser_session.operation import (
    BrowserAuthority,
    OperationAdapter,
    OperationIdentity,
)
from hoyo_browser_session.request_gate import RequestGate

GAME = "zzz"
CURRENT_CONTRACT_VERSION = 1
_LEGACY_CONTRACT_BY_RESULT_SCHEMA = {1: 1, 2: 1}


def _validate_contract_1(result: ExecutorResult) -> None:
    if result.evidence_codes:
        raise ValueError("ZZZ publish result evidence was invalid")


@dataclass(frozen=True, slots=True)
class ZzzPublishOperationAdapter:
    identity: OperationIdentity = field(
        default_factory=lambda: OperationIdentity(GAME, REAL_OPERATION)
    )
    browser_authority: BrowserAuthority = BrowserAuthority.ROUTINE
    current_contract_version: int = CURRENT_CONTRACT_VERSION

    def validate_parameters(self, parameters: JobParameters) -> None:
        del parameters

    async def execute(
        self,
        job: Job,
        settings: Settings,
        browser: BrowserOwnerSession,
        request_gate: RequestGate,
    ) -> ExecutorResult:
        from .cli import execute_owner_job

        if not isinstance(browser, BorrowedBrowserSession):
            raise TypeError("ZZZ Owner executor received login authority")
        result = await execute_owner_job(
            job=job,
            settings=settings,
            browser=browser,
            request_gate=request_gate,
        )
        if not isinstance(result, ExecutorResult):
            raise TypeError("ZZZ Owner executor result was invalid")
        return result

    def legacy_contract_version(self, result_schema_version: int) -> int:
        try:
            return _LEGACY_CONTRACT_BY_RESULT_SCHEMA[result_schema_version]
        except KeyError as error:
            raise ValueError("ZZZ legacy result schema was unsupported") from error

    def validate_live_result(self, result: ExecutorResult) -> None:
        _validate_contract_1(result)

    def validate_persisted_result(
        self,
        result: ExecutorResult,
        contract_version: int,
    ) -> None:
        if contract_version != 1:
            raise ValueError("ZZZ operation contract version was unsupported")
        _validate_contract_1(result)


ZZZ_PUBLISH_ADAPTER: OperationAdapter = ZzzPublishOperationAdapter()
