"""Single execution identity and operation contract authority."""

from __future__ import annotations

import os
from pathlib import Path

from .config import (
    SUPPORTED_GAMES,
    SYNTHETIC_OPERATION_CONTRACT_VERSION,
    SYNTHETIC_OPERATIONS,
    Settings,
)
from .errors import BrokerDatabaseError, ConfigurationError
from .models import ExecutorResult, Job, JobResult
from .operation import OperationAdapter
from .operation_registry import (
    operation_adapter,
    validate_live_operation_result,
    validate_persisted_operation_result,
)
from .repository_identity import (
    RepositoryExecutionIdentity,
    RepositoryIdentityError,
    repository_execution_generation,
    validate_repository_identity,
)


class ExecutionIdentityAuthority:
    """Calculate and validate every repository execution generation."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def validate_request(
        self,
        game: str,
        operation: str,
        repo_root: Path,
        *,
        runtime_digest: str | None = None,
    ) -> tuple[Path, OperationAdapter | None, str]:
        try:
            repository = validate_repository_identity(repo_root)
        except RepositoryIdentityError as error:
            raise ConfigurationError("repository execution identity was rejected") from error
        if game not in SUPPORTED_GAMES:
            raise ConfigurationError("game is not registered")
        adapter = operation_adapter(game, operation)
        allowed = adapter is not None or (
            self.settings.test_mode and operation in SYNTHETIC_OPERATIONS
        )
        if not allowed:
            raise ConfigurationError("operation is not registered")
        try:
            active_digest = (
                self.active_runtime_digest()
                if runtime_digest is None
                else runtime_digest
            )
            contract_version = (
                adapter.current_contract_version
                if adapter is not None
                else SYNTHETIC_OPERATION_CONTRACT_VERSION
            )
            execution_identity = RepositoryExecutionIdentity(
                repository=repository.logical_identity,
                account_scope=self.settings.account_scope,
                game=game,
                operation=operation,
                runtime_digest=active_digest,
                operation_contract_version=contract_version,
            )
        except ValueError as error:
            raise ConfigurationError("repository execution identity was invalid") from error
        return repository.root, adapter, execution_identity.dedupe_key

    def active_runtime_digest(self) -> str:
        if self.settings.test_mode and not os.path.lexists(
            self.settings.active_runtime_file
        ):
            return "0" * 64
        from .runtime import read_active_runtime

        return str(read_active_runtime(self.settings)["runtime_digest"])

    def job_matches(
        self,
        job: Job,
        *,
        runtime_digest: str,
    ) -> bool:
        try:
            root, _, dedupe_key = self.validate_request(
                job.game,
                job.operation,
                job.repo_root,
                runtime_digest=runtime_digest,
            )
        except ConfigurationError:
            return False
        return root == job.repo_root and dedupe_key == job.dedupe_key

    def job_contract_version(self, job: Job) -> int:
        generation = repository_execution_generation(job.dedupe_key)
        if generation is not None:
            return generation[1]
        legacy_key = (
            job.dedupe_key
            == f"hoyo-game-data:{job.account_scope}:{job.game}:{job.operation}"
            or (
                job.dedupe_key.startswith("repository-execution/v1:")
                and len(job.dedupe_key) == len("repository-execution/v1:") + 64
            )
        )
        if not legacy_key:
            raise BrokerDatabaseError("queued Job execution identity was invalid")
        adapter = operation_adapter(job.game, job.operation)
        if adapter is not None:
            return adapter.legacy_contract_version(1)
        if self.settings.test_mode and job.operation in SYNTHETIC_OPERATIONS:
            return SYNTHETIC_OPERATION_CONTRACT_VERSION
        raise BrokerDatabaseError("queued Job operation contract was unavailable")

    def validate_live_result(
        self,
        job: Job,
        result: ExecutorResult,
    ) -> int:
        if operation_adapter(job.game, job.operation) is not None:
            return validate_live_operation_result(job.game, job.operation, result)
        if self.settings.test_mode and job.operation in SYNTHETIC_OPERATIONS:
            return SYNTHETIC_OPERATION_CONTRACT_VERSION
        raise ValueError("job operation was not registered")

    def validate_persisted_result(self, result: JobResult) -> int:
        if operation_adapter(result.game, result.operation) is not None:
            return validate_persisted_operation_result(result)
        if self.settings.test_mode and result.operation in SYNTHETIC_OPERATIONS:
            contract_version = result.operation_contract_version
            if contract_version is None and result.schema_version in {1, 2}:
                contract_version = SYNTHETIC_OPERATION_CONTRACT_VERSION
            if contract_version != SYNTHETIC_OPERATION_CONTRACT_VERSION:
                raise ValueError("synthetic operation contract version was invalid")
            return contract_version
        raise ValueError("result operation was not registered")
