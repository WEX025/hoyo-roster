"""Private fixed-path request and durable result protocol for executor children."""

from __future__ import annotations

import hmac
import json
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from .config import (
    SYNTHETIC_OPERATION_CONTRACT_VERSION,
    SYNTHETIC_OPERATIONS,
    Settings,
)
from .errors import ProtocolStateError
from .locks import FileLock, ensure_private_directory, ensure_private_file
from .models import (
    ExecutorResult,
    Job,
    JobParameters,
    JobResult,
    JobState,
    StableCode,
    SubmitDisposition,
    result_json,
)
from .operation_registry import (
    operation_adapter,
    validate_live_operation_result,
    validate_persisted_operation_result,
)
from .parameter_fingerprint import parameter_fingerprint

REQUEST_SCHEMA_VERSION = 1
_REQUEST_KEYS = frozenset(
    {
        "schema_version",
        "job_id",
        "game",
        "operation",
        "uid",
        "headed",
        "request_interval",
    }
)
_PROTOCOL_NAME_RE = re.compile(
    r"^(?P<temporary>\.)?"
    r"(?P<job_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})"
    r"\.(?P<kind>request|result)\.json(?P<suffix>\.tmp)?$"
)


class ResultFileMissing(ValueError):
    pass


class ResultFileInvalid(ValueError):
    pass


class ResultFileUnsafe(ResultFileInvalid):
    pass


class ResultFileIdentityMismatch(ResultFileInvalid):
    pass


@dataclass(frozen=True, slots=True)
class ProtocolEntry:
    path: Path
    job_id: str
    kind: Literal["request", "result"]
    temporary: bool


@dataclass(frozen=True, slots=True)
class ProtocolInventory:
    """Read-only classification of the complete broker protocol directory."""

    entries: tuple[ProtocolEntry, ...]
    safe_job_ids: frozenset[str]
    unsafe_or_unclassified_count: int

    @property
    def safe_entry_count(self) -> int:
        return len(self.entries)


def _canonical_job_id(job_id: str) -> str:
    canonical = str(uuid.UUID(job_id))
    if canonical != job_id:
        raise ValueError("job ID must be a canonical UUID")
    return canonical


def request_path(settings: Settings, job_id: str) -> Path:
    return settings.broker_tmp / f"{_canonical_job_id(job_id)}.request.json"


def result_paths(settings: Settings, job_id: str) -> tuple[Path, Path]:
    canonical = _canonical_job_id(job_id)
    return (
        settings.broker_tmp / f"{canonical}.result.json",
        settings.broker_tmp / f".{canonical}.result.json.tmp",
    )


def protocol_entry(path: Path) -> ProtocolEntry:
    match = _PROTOCOL_NAME_RE.fullmatch(path.name)
    if match is None:
        raise ProtocolStateError("broker tmp contains an unknown protocol entry")
    prefix_temporary = match.group("temporary") == "."
    suffix_temporary = match.group("suffix") == ".tmp"
    if prefix_temporary != suffix_temporary:
        raise ProtocolStateError("broker tmp protocol filename is invalid")
    job_id = match.group("job_id")
    try:
        _canonical_job_id(job_id)
    except ValueError as error:
        raise ProtocolStateError("broker tmp Job ID is invalid") from error
    kind = match.group("kind")
    if kind not in {"request", "result"}:
        raise ProtocolStateError("broker tmp protocol kind is invalid")
    return ProtocolEntry(
        path=path,
        job_id=job_id,
        kind=cast(Literal["request", "result"], kind),
        temporary=prefix_temporary,
    )


def _validate_private_regular_file(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ProtocolStateError("protocol file could not be inspected") from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ProtocolStateError("protocol file ownership, type, or mode is unsafe")
    return metadata


def validate_protocol_entry(entry: ProtocolEntry) -> None:
    _validate_private_regular_file(entry.path)


def classify_protocol_inventory(
    settings: Settings,
    *,
    strict: bool,
) -> ProtocolInventory:
    """Classify protocol files once, failing strict maintenance on any unsafe entry."""

    try:
        paths = sorted(settings.broker_tmp.iterdir(), key=lambda path: path.name)
    except FileNotFoundError:
        paths = []
    except OSError as error:
        raise ProtocolStateError("broker protocol inventory could not be read") from error
    entries: list[ProtocolEntry] = []
    unsafe_or_unclassified_count = 0
    for path in paths:
        if not os.path.lexists(path):
            continue
        try:
            entry = protocol_entry(path)
            validate_protocol_entry(entry)
        except ProtocolStateError:
            if strict:
                raise
            unsafe_or_unclassified_count += 1
            continue
        entries.append(entry)
    return ProtocolInventory(
        entries=tuple(entries),
        safe_job_ids=frozenset(entry.job_id for entry in entries),
        unsafe_or_unclassified_count=unsafe_or_unclassified_count,
    )


def _stage_private_json(
    temporary: Path,
    final: Path,
    payload: dict[str, Any],
) -> None:
    if os.path.lexists(temporary) or os.path.lexists(final):
        raise FileExistsError("private protocol file already exists")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if os.path.lexists(temporary):
            os.unlink(temporary)
        raise


def _write_private_json(
    temporary: Path,
    final: Path,
    payload: dict[str, Any],
) -> None:
    _stage_private_json(temporary, final, payload)
    try:
        os.replace(temporary, final)
    except BaseException:
        if os.path.lexists(temporary):
            os.unlink(temporary)
        raise


def write_job_request(
    settings: Settings,
    *,
    job_id: str,
    game: str,
    operation: str,
    parameters: JobParameters,
) -> Path:
    if operation_adapter(game, operation) is None:
        raise ValueError("Owner job request is not registered")
    ensure_private_directory(settings.broker_root)
    ensure_private_directory(settings.broker_tmp)
    final = request_path(settings, job_id)
    temporary = final.with_name(f".{final.name}.tmp")
    payload = {
        "game": game,
        "headed": parameters.headed,
        "job_id": job_id,
        "operation": operation,
        "request_interval": parameters.request_interval,
        "schema_version": REQUEST_SCHEMA_VERSION,
        "uid": parameters.uid,
    }
    _write_private_json(temporary, final, payload)
    return final


def read_job_request(
    settings: Settings,
    *,
    job_id: str,
    game: str,
    operation: str,
    expected_digest: str,
) -> JobParameters:
    path = request_path(settings, job_id)
    try:
        _validate_private_regular_file(path)
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except ProtocolStateError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("job request file is unavailable") from error
    if not isinstance(payload, dict) or set(payload) != _REQUEST_KEYS:
        raise ValueError("job request fields are invalid")
    if (
        payload.get("schema_version") != REQUEST_SCHEMA_VERSION
        or payload.get("job_id") != job_id
        or payload.get("game") != game
        or payload.get("operation") != operation
    ):
        raise ValueError("job request identity is invalid")
    uid = payload.get("uid")
    if uid is not None and type(uid) is not str:
        raise ValueError("job request UID is invalid")
    headed = payload.get("headed")
    if type(headed) is not bool:
        raise ValueError("job request headed mode is invalid")
    request_interval = payload.get("request_interval")
    if isinstance(request_interval, bool) or not isinstance(request_interval, int | float):
        raise ValueError("job request interval is invalid")
    parameters = JobParameters(
        uid=uid,
        headed=headed,
        request_interval=request_interval,
    )
    actual_digest = parameter_fingerprint(settings, parameters)
    if not hmac.compare_digest(actual_digest, expected_digest):
        raise ValueError("job request parameter digest is invalid")
    return parameters


def read_worker_result(
    settings: Settings,
    job: Job,
) -> JobResult:
    final, _ = result_paths(settings, job.job_id)
    if not os.path.lexists(final):
        raise ResultFileMissing("worker result file is missing")
    try:
        _validate_private_regular_file(final)
    except ProtocolStateError as error:
        raise ResultFileUnsafe("worker result file is unsafe") from error
    try:
        persisted = JobResult.from_json(
            final.read_text(encoding="utf-8"),
            disposition=SubmitDisposition.CREATED,
        )
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise ResultFileInvalid("worker result file is invalid") from error
    if (
        persisted.job_id != job.job_id
        or persisted.game != job.game
        or persisted.operation != job.operation
    ):
        raise ResultFileIdentityMismatch("worker result identity is invalid")
    if persisted.status not in {JobState.SUCCEEDED, JobState.FAILED}:
        raise ResultFileInvalid("worker result status is invalid")
    if persisted.stable_code in {
        StableCode.PUBLISH_OUTCOME_UNKNOWN,
        StableCode.PUBLISH_OUTCOME_UNKNOWN_ACKNOWLEDGED,
    }:
        raise ResultFileInvalid("worker result stable code is invalid")
    adapter = operation_adapter(job.game, job.operation)
    if adapter is None and not (settings.test_mode and job.operation in SYNTHETIC_OPERATIONS):
        raise ResultFileInvalid("worker result operation is not registered")
    if adapter is not None:
        try:
            validate_persisted_operation_result(persisted)
        except ValueError as error:
            raise ResultFileInvalid("worker result operation contract is invalid") from error
    return persisted


def executor_result_from_job_result(result: JobResult) -> ExecutorResult:
    return ExecutorResult(
        stable_code=result.stable_code,
        request_count=result.request_count,
        no_op=result.no_op,
        data_modified=result.data_modified,
        commit_sha=result.commit_sha,
        evidence_codes=result.evidence_codes,
    )


def write_worker_result(
    settings: Settings,
    *,
    job_id: str,
    game: str,
    operation: str,
    result: ExecutorResult,
) -> None:
    adapter = operation_adapter(game, operation)
    if adapter is None and not (settings.test_mode and operation in SYNTHETIC_OPERATIONS):
        raise ValueError("Owner job result is not registered")
    if adapter is not None:
        contract_version = validate_live_operation_result(game, operation, result)
    else:
        contract_version = SYNTHETIC_OPERATION_CONTRACT_VERSION
    ensure_private_directory(settings.broker_tmp)
    final, temporary = result_paths(settings, job_id)
    job = Job(
        job_id=job_id,
        account_scope=settings.account_scope,
        game=game,
        operation=operation,
        repo_root=Path("."),
        dedupe_key=f"hoyo-game-data:{settings.account_scope}:{game}:{operation}",
        parameter_digest="0" * 64,
        force=False,
        state=JobState.RUNNING,
        created_at=0,
        not_before_at=0,
        started_at=0,
    )
    status = JobState.SUCCEEDED if result.stable_code is StableCode.PASS else JobState.FAILED
    payload = json.loads(
        result_json(
            job=job,
            status=status,
            result=result,
            completed_at=time.time(),
            operation_contract_version=contract_version,
        )
    )
    # Any code holding both authorities must acquire protocol.lock before SQLite.
    # Child execution and network work are complete before this lock is acquired.
    _stage_private_json(temporary, final, payload)
    ensure_private_file(settings.protocol_lock_file)
    protocol_lock = FileLock(settings.protocol_lock_file)
    protocol_lock.acquire(blocking=True)
    try:
        if os.path.lexists(final):
            raise FileExistsError("private protocol file already exists")
        os.replace(temporary, final)
    except BaseException:
        if os.path.lexists(temporary):
            temporary.unlink()
        raise
    finally:
        protocol_lock.release()


def validate_job_protocol_files(settings: Settings, job_id: str) -> None:
    request = request_path(settings, job_id)
    final, temporary = result_paths(settings, job_id)
    request_temporary = request.with_name(f".{request.name}.tmp")
    for path in (request, request_temporary, final, temporary):
        if os.path.lexists(path):
            _validate_private_regular_file(path)


def cleanup_job_protocol_files(settings: Settings, job_id: str) -> None:
    request = request_path(settings, job_id)
    final, temporary = result_paths(settings, job_id)
    request_temporary = request.with_name(f".{request.name}.tmp")
    paths = (request, request_temporary, final, temporary)
    validate_job_protocol_files(settings, job_id)
    removed = False
    for path in paths:
        if not os.path.lexists(path):
            continue
        try:
            path.unlink()
        except OSError as error:
            raise ProtocolStateError("protocol file cleanup failed") from error
        removed = True
    if removed:
        directory = os.open(settings.broker_tmp, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
