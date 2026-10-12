"""Fixed shared-session login operation adapter."""

from __future__ import annotations

from dataclasses import dataclass, field

from .browser import BrowserOwnerSession, LoginBrowserSession, navigate_login_page
from .config import LOGIN_OPERATION, SESSION_GAME, Settings
from .login import LOGIN_GAMES, LoginCoverageStatus
from .models import ExecutorResult, Job, JobParameters, StableCode
from .operation import (
    BrowserAuthority,
    OperationAdapter,
    OperationIdentity,
)
from .request_gate import RequestGate

CURRENT_CONTRACT_VERSION = 2
ACCOUNT_LOGIN_URL = "https://user.mihoyo.com/"
_LEGACY_CONTRACT_BY_RESULT_SCHEMA = {1: 1, 2: 1}


def _validate_contract_1(result: ExecutorResult) -> None:
    if result.evidence_codes:
        raise ValueError("login result evidence was invalid")


def _validate_contract_2(result: ExecutorResult) -> None:
    if result.stable_code is not StableCode.PASS:
        _validate_contract_1(result)
        return
    statuses = {LoginCoverageStatus.PASS, LoginCoverageStatus.EXPECTED_IDENTITY_UNAVAILABLE}
    allowed = {
        f"{game.upper()}_BINDING_{status.value}" for game in LOGIN_GAMES for status in statuses
    }
    coverage = tuple(code for code in result.evidence_codes if code in allowed)
    if (
        len(coverage) != len(LOGIN_GAMES)
        or any(
            sum(code.startswith(f"{game.upper()}_BINDING_") for code in coverage) != 1
            for game in LOGIN_GAMES
        )
        or not any(code.endswith("_PASS") for code in coverage)
        or set(result.evidence_codes) - allowed - {"LOGIN_WAITING_SIGNAL_EMITTED"}
    ):
        raise ValueError("login result coverage evidence was invalid")


@dataclass(frozen=True, slots=True)
class LoginPrimaryOperationAdapter:
    identity: OperationIdentity = field(
        default_factory=lambda: OperationIdentity(
            SESSION_GAME,
            LOGIN_OPERATION,
        )
    )
    browser_authority: BrowserAuthority = BrowserAuthority.LOGIN
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
        from .login import coordinate_login
        from .login_registry import create_fixed_login_providers
        from .queue import JobQueue
        from .worker_protocol import read_job_request

        if not isinstance(browser, LoginBrowserSession):
            raise TypeError("Login executor requires explicit login authority")
        parameters = read_job_request(
            settings,
            job_id=job.job_id,
            game=self.identity.game,
            operation=self.identity.operation,
            expected_digest=job.parameter_digest,
        )
        providers = create_fixed_login_providers(
            context=browser.context,
            page=browser.idle_page,
            request_gate=request_gate,
            request_interval=parameters.request_interval,
            bootstrap_identity=parameters.uid,
        )
        queue = JobQueue(settings)

        async def open_account_login() -> None:
            await request_gate.reserve(requested_min_interval=parameters.request_interval)
            await navigate_login_page(browser.idle_page, ACCOUNT_LOGIN_URL)

        result = await coordinate_login(
            repo_root=job.repo_root,
            context=browser.context,
            page=browser.idle_page,
            request_gate=request_gate,
            providers=providers,
            headed=True,
            poll_interval=settings.login_probe_interval_seconds,
            timeout=settings.login_timeout_seconds,
            waiting_callback=lambda: queue.mark_login_waiting(job.job_id),
            open_login_first=True,
            interactive_login=open_account_login,
        )
        if not result.available_coverage_success:
            return ExecutorResult(stable_code=StableCode.AUTHENTICATION_REQUIRED)
        queue.clear_authentication_block()
        evidence = [f"{item.game.upper()}_BINDING_{item.status.value}" for item in result.coverage]
        if result.waiting_signal_count:
            evidence.append("LOGIN_WAITING_SIGNAL_EMITTED")
        return ExecutorResult(stable_code=StableCode.PASS, evidence_codes=tuple(sorted(evidence)))

    def legacy_contract_version(self, result_schema_version: int) -> int:
        try:
            return _LEGACY_CONTRACT_BY_RESULT_SCHEMA[result_schema_version]
        except KeyError as error:
            raise ValueError("login legacy result schema was unsupported") from error

    def validate_live_result(self, result: ExecutorResult) -> None:
        _validate_contract_2(result)

    def validate_persisted_result(
        self,
        result: ExecutorResult,
        contract_version: int,
    ) -> None:
        if contract_version == 1:
            _validate_contract_1(result)
        elif contract_version == 2:
            _validate_contract_2(result)
        else:
            raise ValueError("login operation contract version was unsupported")


LOGIN_PRIMARY_ADAPTER: OperationAdapter = LoginPrimaryOperationAdapter()
