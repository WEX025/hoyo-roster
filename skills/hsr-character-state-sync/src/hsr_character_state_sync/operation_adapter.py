"""Fixed HSR Owner operations and domain result validation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from hoyo_browser_session.browser import BorrowedBrowserSession, BrowserOwnerSession
from hoyo_browser_session.config import Settings
from hoyo_browser_session.errors import ConfigurationError
from hoyo_browser_session.models import ExecutorResult, Job, JobParameters
from hoyo_browser_session.operation import (
    BrowserAuthority,
    OperationAdapter,
    OperationIdentity,
)
from hoyo_browser_session.request_gate import RequestGate

from .evidence import (
    BOOTSTRAP_OPERATION,
    CURRENT_CONTRACT_VERSION,
    DRY_RUN_OPERATION,
    GAME,
    PUBLISH_OPERATION,
    validate_hsr_operation_result,
)

HsrExecutor = Callable[
    [Job, Settings, BorrowedBrowserSession, RequestGate],
    Awaitable[ExecutorResult],
]
_LEGACY_CONTRACT_BY_RESULT_SCHEMA = {1: 1, 2: 1}


async def _execute_publish(
    job: Job,
    settings: Settings,
    browser: BorrowedBrowserSession,
    request_gate: RequestGate,
) -> ExecutorResult:
    from .cli import execute_owner_publish_job

    return await execute_owner_publish_job(
        job=job,
        settings=settings,
        browser=browser,
        request_gate=request_gate,
    )


async def _execute_bootstrap(
    job: Job,
    settings: Settings,
    browser: BorrowedBrowserSession,
    request_gate: RequestGate,
) -> ExecutorResult:
    from .cli import execute_owner_bootstrap_job

    return await execute_owner_bootstrap_job(
        job=job,
        settings=settings,
        browser=browser,
        request_gate=request_gate,
    )


async def _execute_dry_run(
    job: Job,
    settings: Settings,
    browser: BorrowedBrowserSession,
    request_gate: RequestGate,
) -> ExecutorResult:
    from .cli import execute_owner_dry_run_job

    return await execute_owner_dry_run_job(
        job=job,
        settings=settings,
        browser=browser,
        request_gate=request_gate,
    )


@dataclass(frozen=True, slots=True)
class HsrOperationAdapter:
    identity: OperationIdentity
    executor: HsrExecutor = field(repr=False)
    browser_authority: BrowserAuthority = BrowserAuthority.ROUTINE
    current_contract_version: int = CURRENT_CONTRACT_VERSION

    def validate_parameters(self, parameters: JobParameters) -> None:
        if parameters.uid is None:
            raise ConfigurationError("HSR jobs require an explicit UID")

    async def execute(
        self,
        job: Job,
        settings: Settings,
        browser: BrowserOwnerSession,
        request_gate: RequestGate,
    ) -> ExecutorResult:
        if not isinstance(browser, BorrowedBrowserSession):
            raise TypeError("HSR Owner executor received login authority")
        result = await self.executor(job, settings, browser, request_gate)
        if not isinstance(result, ExecutorResult):
            raise TypeError("HSR Owner executor result was invalid")
        return result

    def legacy_contract_version(self, result_schema_version: int) -> int:
        try:
            return _LEGACY_CONTRACT_BY_RESULT_SCHEMA[result_schema_version]
        except KeyError as error:
            raise ValueError("HSR legacy result schema was unsupported") from error

    def validate_live_result(self, result: ExecutorResult) -> None:
        validate_hsr_operation_result(
            self.identity.operation,
            result,
            contract_version=self.current_contract_version,
        )

    def validate_persisted_result(
        self,
        result: ExecutorResult,
        contract_version: int,
    ) -> None:
        validate_hsr_operation_result(
            self.identity.operation,
            result,
            contract_version=contract_version,
        )


HSR_PUBLISH_ADAPTER: OperationAdapter = HsrOperationAdapter(
    OperationIdentity(GAME, PUBLISH_OPERATION),
    _execute_publish,
)
HSR_BOOTSTRAP_ADAPTER: OperationAdapter = HsrOperationAdapter(
    OperationIdentity(GAME, BOOTSTRAP_OPERATION),
    _execute_bootstrap,
)
HSR_DRY_RUN_ADAPTER: OperationAdapter = HsrOperationAdapter(
    OperationIdentity(GAME, DRY_RUN_OPERATION),
    _execute_dry_run,
)
