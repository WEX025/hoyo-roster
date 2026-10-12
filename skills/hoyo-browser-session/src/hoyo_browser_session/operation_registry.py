"""Lazy immutable registry interface; application.py owns package composition."""

from __future__ import annotations

from collections.abc import Iterator, Mapping

from .models import ExecutorResult, JobResult
from .operation import OperationAdapter, OperationKey

_FIXED_KEYS: tuple[OperationKey, ...] = (
    ("genshin", "publish-main"),
    ("zzz", "publish-main"),
    ("hsr", "publish-main"),
    ("hsr", "bootstrap-check"),
    ("hsr", "dry-run"),
    ("session", "login-primary"),
)


class _FixedOperationRegistry(Mapping[OperationKey, OperationAdapter]):
    def __getitem__(self, key: OperationKey) -> OperationAdapter:
        if key not in _FIXED_KEYS:
            raise KeyError(key)
        from .application import owner_operation_registry

        return owner_operation_registry()[key]

    def __iter__(self) -> Iterator[OperationKey]:
        return iter(_FIXED_KEYS)

    def __len__(self) -> int:
        return len(_FIXED_KEYS)


OWNER_OPERATION_REGISTRY: Mapping[OperationKey, OperationAdapter] = _FixedOperationRegistry()
OWNER_OPERATIONS = frozenset(_FIXED_KEYS)


def operation_adapter(game: str, operation: str) -> OperationAdapter | None:
    return OWNER_OPERATION_REGISTRY.get((game, operation))


def _required_adapter(game: str, operation: str) -> OperationAdapter:
    adapter = operation_adapter(game, operation)
    if adapter is None:
        raise ValueError("result operation was not registered")
    return adapter


def _executor_result(result: JobResult) -> ExecutorResult:
    return ExecutorResult(
        stable_code=result.stable_code,
        request_count=result.request_count,
        no_op=result.no_op,
        data_modified=result.data_modified,
        commit_sha=result.commit_sha,
        evidence_codes=result.evidence_codes,
    )


def validate_live_operation_result(
    game: str,
    operation: str,
    result: ExecutorResult,
) -> int:
    adapter = _required_adapter(game, operation)
    adapter.validate_live_result(result)
    return adapter.current_contract_version


def validate_persisted_operation_result(result: JobResult) -> int:
    adapter = _required_adapter(result.game, result.operation)
    contract_version = result.operation_contract_version
    if contract_version is None:
        contract_version = adapter.legacy_contract_version(result.schema_version)
    if type(contract_version) is not int or contract_version < 1:
        raise ValueError("persisted operation contract version was invalid")
    adapter.validate_persisted_result(
        _executor_result(result),
        contract_version,
    )
    return contract_version
