"""Strict in-memory and persisted broker models."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

RESULT_SCHEMA_VERSION = 3
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_UID_RE = re.compile(r"^[1-9][0-9]{0,31}$")
_EVIDENCE_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_RESULT_V1_KEYS = frozenset(
    {
        "schema_version",
        "job_id",
        "game",
        "operation",
        "status",
        "stable_code",
        "request_count",
        "no_op",
        "data_modified",
        "commit_sha",
        "completed_at",
    }
)
_RESULT_V2_KEYS = _RESULT_V1_KEYS | frozenset({"evidence_codes"})
_RESULT_V3_KEYS = _RESULT_V2_KEYS | frozenset({"operation_contract_version"})


class JobState(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    ABANDONED = "ABANDONED"


class OwnerPhase(StrEnum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    BROKEN = "BROKEN"
    STOPPED = "STOPPED"


class SubmitDisposition(StrEnum):
    CREATED = "CREATED"
    COALESCED = "COALESCED"
    REUSED = "REUSED"


class AccountState(StrEnum):
    HEALTHY = "HEALTHY"
    COOLDOWN = "COOLDOWN"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"
    RISK_CONTROL = "RISK_CONTROL"
    PROFILE_BROKEN = "PROFILE_BROKEN"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class StableCode(StrEnum):
    PASS = "PASS"
    RUNTIME_NOT_READY = "RUNTIME_NOT_READY"
    BROKER_DB_ERROR = "BROKER_DB_ERROR"
    WORKER_START_FAILED = "WORKER_START_FAILED"
    OWNER_RUNTIME_RESTART_REQUIRED = "OWNER_RUNTIME_RESTART_REQUIRED"
    OWNER_BROKEN = "OWNER_BROKEN"
    OWNER_SHUTDOWN_IN_PROGRESS = "OWNER_SHUTDOWN_IN_PROGRESS"
    OWNER_QUEUE_NOT_EMPTY = "OWNER_QUEUE_NOT_EMPTY"
    EXECUTOR_NOT_REGISTERED = "EXECUTOR_NOT_REGISTERED"
    PROFILE_IN_USE = "PROFILE_IN_USE"
    PROFILE_STALE_SINGLETON = "PROFILE_STALE_SINGLETON"
    PROFILE_BROKEN = "PROFILE_BROKEN"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    LOGIN_TIMEOUT = "LOGIN_TIMEOUT"
    LOGIN_NETWORK_ERROR = "LOGIN_NETWORK_ERROR"
    LOGIN_BINDING_FAILED = "LOGIN_BINDING_FAILED"
    WRONG_ACCOUNT = "WRONG_ACCOUNT"
    RATE_LIMITED = "RATE_LIMITED"
    RISK_CONTROL = "RISK_CONTROL"
    UID_REQUIRED = "UID_REQUIRED"
    PUBLISH_OUTCOME_UNKNOWN = "PUBLISH_OUTCOME_UNKNOWN"
    PUBLISH_OUTCOME_UNKNOWN_ACKNOWLEDGED = "PUBLISH_OUTCOME_UNKNOWN_ACKNOWLEDGED"
    JOB_ABANDONED = "JOB_ABANDONED"
    JOB_FAILED = "JOB_FAILED"
    GENSHIN_SNAPSHOT_DRIFT = "GENSHIN_SNAPSHOT_DRIFT"
    GENSHIN_SOURCE_CONTRACT_FAILED = "GENSHIN_SOURCE_CONTRACT_FAILED"
    GENSHIN_SOURCE_RESPONSE_FAILED = "GENSHIN_SOURCE_RESPONSE_FAILED"
    GENSHIN_FIELD_COVERAGE_REGRESSED = "GENSHIN_FIELD_COVERAGE_REGRESSED"
    GENSHIN_CANDIDATE_VALIDATION_FAILED = "GENSHIN_CANDIDATE_VALIDATION_FAILED"
    GENSHIN_LIFECYCLE_FAILED = "GENSHIN_LIFECYCLE_FAILED"
    GENSHIN_IDENTITY_CHANGED = "GENSHIN_IDENTITY_CHANGED"
    GENSHIN_ROSTER_SHRANK = "GENSHIN_ROSTER_SHRANK"
    GENSHIN_PLAYER_LEVEL_DECREASED = "GENSHIN_PLAYER_LEVEL_DECREASED"
    GENSHIN_FRIENDSHIP_DECREASED = "GENSHIN_FRIENDSHIP_DECREASED"
    GENSHIN_CHARACTER_PROGRESSION_DECREASED = "GENSHIN_CHARACTER_PROGRESSION_DECREASED"
    GENSHIN_CHARACTER_LEVEL_DECREASED = "GENSHIN_CHARACTER_LEVEL_DECREASED"
    GENSHIN_ASCENSION_DECREASED = "GENSHIN_ASCENSION_DECREASED"
    GENSHIN_CONSTELLATION_DECREASED = "GENSHIN_CONSTELLATION_DECREASED"
    GENSHIN_NORMAL_ATTACK_DECREASED = "GENSHIN_NORMAL_ATTACK_DECREASED"
    GENSHIN_ELEMENTAL_SKILL_DECREASED = "GENSHIN_ELEMENTAL_SKILL_DECREASED"
    GENSHIN_ELEMENTAL_BURST_DECREASED = "GENSHIN_ELEMENTAL_BURST_DECREASED"
    GENSHIN_TIMESTAMP_REGRESSED = "GENSHIN_TIMESTAMP_REGRESSED"
    ZZZ_FORMAL_VALIDATION_FAILED = "ZZZ_FORMAL_VALIDATION_FAILED"
    ZZZ_CANDIDATE_VALIDATION_FAILED = "ZZZ_CANDIDATE_VALIDATION_FAILED"
    ZZZ_CANDIDATE_CLEANUP_FAILED = "ZZZ_CANDIDATE_CLEANUP_FAILED"
    ZZZ_APPLY_VALIDATION_FAILED = "ZZZ_APPLY_VALIDATION_FAILED"
    ZZZ_APPLY_FAILED = "ZZZ_APPLY_FAILED"
    ZZZ_PUBLISH_FAILED = "ZZZ_PUBLISH_FAILED"
    ZZZ_REMOTE_MAIN_MOVED = "ZZZ_REMOTE_MAIN_MOVED"
    ZZZ_MAIN_PUSH_REJECTED = "ZZZ_MAIN_PUSH_REJECTED"
    ZZZ_MAPPING_FAILED = "ZZZ_MAPPING_FAILED"
    ZZZ_SNAPSHOT_DRIFT = "ZZZ_SNAPSHOT_DRIFT"
    ZZZ_LIFECYCLE_FAILED = "ZZZ_LIFECYCLE_FAILED"
    ZZZ_SOURCE_TRANSPORT_FAILED = "ZZZ_SOURCE_TRANSPORT_FAILED"
    ZZZ_SOURCE_RESPONSE_FAILED = "ZZZ_SOURCE_RESPONSE_FAILED"
    JOB_COALESCED = "JOB_COALESCED"
    JOB_REUSED = "JOB_REUSED"
    JOB_WAIT_TIMEOUT = "JOB_WAIT_TIMEOUT"
    NOT_RUN_MISSING_RUNTIME = "NOT_RUN_MISSING_RUNTIME"


_BLOCKED_RESULT_CODES = frozenset(
    {
        StableCode.AUTHENTICATION_REQUIRED,
        StableCode.RATE_LIMITED,
        StableCode.RISK_CONTROL,
        StableCode.PROFILE_BROKEN,
        StableCode.PUBLISH_OUTCOME_UNKNOWN,
    }
)


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
        if self.status is LoginProbeStatus.PASS and not self.identity:
            raise ValueError("a successful login probe requires an identity")
        if self.status is not LoginProbeStatus.PASS and self.identity is not None:
            raise ValueError("only a successful login probe may carry an identity")


@dataclass(frozen=True, slots=True)
class JobParameters:
    """Strict game inputs persisted only in a private short-lived request file."""

    uid: str | None
    headed: bool
    request_interval: float

    def __post_init__(self) -> None:
        if self.uid is not None and _UID_RE.fullmatch(self.uid) is None:
            raise ValueError("job UID must be a canonical positive decimal ID")
        if type(self.headed) is not bool:
            raise ValueError("job headed mode must be boolean")
        if (
            isinstance(self.request_interval, bool)
            or not isinstance(self.request_interval, int | float)
            or not math.isfinite(self.request_interval)
            or self.request_interval < 1.0
        ):
            raise ValueError("job request interval must be at least one second")

    def canonical_bytes(self) -> bytes:
        payload = {
            "headed": self.headed,
            "request_interval": self.request_interval,
            "uid": self.uid,
        }
        return json.dumps(
            payload,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()


@dataclass(frozen=True, slots=True)
class Job:
    job_id: str
    account_scope: str
    game: str
    operation: str
    repo_root: Path = field(repr=False)
    dedupe_key: str
    parameter_digest: str
    force: bool
    state: JobState
    created_at: float
    not_before_at: float
    started_at: float | None = None


@dataclass(frozen=True, slots=True)
class ExecutorResult:
    stable_code: StableCode
    request_count: int = 0
    no_op: bool = False
    data_modified: bool = False
    commit_sha: str | None = None
    evidence_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.request_count < 0:
            raise ValueError("request count cannot be negative")
        if self.commit_sha is not None and _SHA_RE.fullmatch(self.commit_sha) is None:
            raise ValueError("commit SHA must be an exact lowercase Git object ID")
        if self.stable_code is not StableCode.PASS and self.data_modified:
            raise ValueError("a failed executor cannot report modified formal data")
        if self.no_op and self.data_modified:
            raise ValueError("a no-op executor cannot modify formal data")
        if self.no_op and self.commit_sha is not None:
            raise ValueError("a no-op executor cannot report a commit")
        if self.data_modified != (self.commit_sha is not None):
            raise ValueError("formal data modification and commit SHA must agree")
        _validate_evidence_container(self.stable_code, self.evidence_codes)


@dataclass(frozen=True, slots=True)
class JobResult:
    job_id: str
    game: str
    operation: str
    schema_version: int
    operation_contract_version: int | None
    status: JobState
    stable_code: StableCode
    disposition: SubmitDisposition
    request_count: int
    no_op: bool
    data_modified: bool
    commit_sha: str | None
    completed_at: float
    evidence_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_persisted_contract_fields(
            schema_version=self.schema_version,
            operation_contract_version=self.operation_contract_version,
        )
        _validate_result_consistency(
            status=self.status,
            stable_code=self.stable_code,
            no_op=self.no_op,
            data_modified=self.data_modified,
            commit_sha=self.commit_sha,
        )
        _validate_evidence_container(self.stable_code, self.evidence_codes)

    @property
    def submission_code(self) -> StableCode:
        if self.disposition is SubmitDisposition.COALESCED:
            return StableCode.JOB_COALESCED
        if self.disposition is SubmitDisposition.REUSED:
            return StableCode.JOB_REUSED
        return self.stable_code

    @classmethod
    def from_json(
        cls,
        value: str,
        *,
        disposition: SubmitDisposition,
    ) -> JobResult:
        payload = strict_result_payload(value)
        return cls(
            job_id=payload["job_id"],
            game=payload["game"],
            operation=payload["operation"],
            schema_version=payload["schema_version"],
            operation_contract_version=payload["operation_contract_version"],
            status=JobState(payload["status"]),
            stable_code=StableCode(payload["stable_code"]),
            disposition=disposition,
            request_count=payload["request_count"],
            no_op=payload["no_op"],
            data_modified=payload["data_modified"],
            commit_sha=payload["commit_sha"],
            completed_at=payload["completed_at"],
            evidence_codes=tuple(payload["evidence_codes"]),
        )


@dataclass(frozen=True, slots=True)
class Submission:
    job_id: str
    disposition: SubmitDisposition
    reused_result: JobResult | None = None


@dataclass(frozen=True, slots=True)
class ClaimOutcome:
    job: Job | None
    state_changed: bool
    next_check_at: float | None = None


@dataclass(frozen=True, slots=True)
class OwnerRecord:
    owner_id: str | None
    pid: int | None = field(default=None, repr=False)
    heartbeat_at: float | None = None
    runtime_digest: str | None = None
    phase: OwnerPhase = OwnerPhase.STOPPED
    browser_context_ready: bool = False
    context_generation: str | None = None


def result_json(
    *,
    job: Job,
    status: JobState,
    result: ExecutorResult,
    completed_at: float,
    operation_contract_version: int,
) -> str:
    _validate_operation_contract_version(operation_contract_version)
    _validate_result_consistency(
        status=status,
        stable_code=result.stable_code,
        no_op=result.no_op,
        data_modified=result.data_modified,
        commit_sha=result.commit_sha,
    )
    _validate_evidence_container(result.stable_code, result.evidence_codes)
    payload: dict[str, object] = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "job_id": job.job_id,
        "game": job.game,
        "operation": job.operation,
        "operation_contract_version": operation_contract_version,
        "status": status.value,
        "stable_code": result.stable_code.value,
        "request_count": result.request_count,
        "no_op": result.no_op,
        "data_modified": result.data_modified,
        "commit_sha": result.commit_sha,
        "completed_at": completed_at,
        "evidence_codes": list(result.evidence_codes),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _validate_result_consistency(
    *,
    status: JobState,
    stable_code: StableCode,
    no_op: bool,
    data_modified: bool,
    commit_sha: str | None,
) -> None:
    succeeded = status is JobState.SUCCEEDED
    passed = stable_code is StableCode.PASS
    invalid = succeeded != passed
    invalid = invalid or (stable_code is not StableCode.PASS and data_modified)
    invalid = invalid or (no_op and data_modified)
    invalid = invalid or (no_op and commit_sha is not None)
    invalid = invalid or (data_modified != (commit_sha is not None))
    invalid = invalid or (status is JobState.BLOCKED and stable_code not in _BLOCKED_RESULT_CODES)
    invalid = invalid or (
        (status is JobState.ABANDONED) != (stable_code is StableCode.JOB_ABANDONED)
    )
    if invalid:
        raise ValueError("result JSON consistency was invalid")


def _validate_evidence_sequence(evidence_codes: tuple[str, ...]) -> None:
    if type(evidence_codes) is not tuple or any(
        type(code) is not str or _EVIDENCE_CODE_RE.fullmatch(code) is None
        for code in evidence_codes
    ):
        raise ValueError("result evidence codes were invalid")
    if len(set(evidence_codes)) != len(evidence_codes):
        raise ValueError("result evidence codes contained a duplicate")
    if evidence_codes != tuple(sorted(evidence_codes)):
        raise ValueError("result evidence codes were not stably sorted")


def _validate_evidence_container(
    stable_code: StableCode,
    evidence_codes: tuple[str, ...],
) -> None:
    _validate_evidence_sequence(evidence_codes)
    if stable_code is not StableCode.PASS and evidence_codes:
        raise ValueError("failed result evidence codes were invalid")


def _validate_operation_contract_version(value: object) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("operation contract version was invalid")
    return value


def _validate_persisted_contract_fields(
    *,
    schema_version: int,
    operation_contract_version: int | None,
) -> None:
    if schema_version in {1, 2}:
        if operation_contract_version is not None:
            raise ValueError("legacy result operation contract version was invalid")
        return
    if schema_version == RESULT_SCHEMA_VERSION:
        _validate_operation_contract_version(operation_contract_version)
        return
    raise ValueError("result JSON schema version was invalid")


def strict_result_payload(value: str) -> dict[str, Any]:
    try:
        payload: Any = json.loads(value)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("result JSON was invalid") from error
    if not isinstance(payload, dict):
        raise ValueError("result JSON fields were invalid")
    schema_version = payload.get("schema_version")
    if type(schema_version) is not int:
        raise ValueError("result JSON schema version was invalid")
    expected_keys = (
        _RESULT_V1_KEYS
        if schema_version == 1
        else _RESULT_V2_KEYS
        if schema_version == 2
        else _RESULT_V3_KEYS
        if schema_version == RESULT_SCHEMA_VERSION
        else None
    )
    if expected_keys is None:
        raise ValueError("result JSON schema version was invalid")
    if set(payload) != expected_keys:
        raise ValueError("result JSON fields were invalid")
    string_fields = ("job_id", "game", "operation", "status", "stable_code")
    if any(type(payload.get(key)) is not str or not payload[key] for key in string_fields):
        raise ValueError("result JSON string fields were invalid")
    status = JobState(payload["status"])
    stable_code = StableCode(payload["stable_code"])
    if type(payload.get("request_count")) is not int or payload["request_count"] < 0:
        raise ValueError("result JSON request count was invalid")
    if type(payload.get("no_op")) is not bool or type(payload.get("data_modified")) is not bool:
        raise ValueError("result JSON boolean fields were invalid")
    commit_sha = payload.get("commit_sha")
    if commit_sha is not None and (
        type(commit_sha) is not str or _SHA_RE.fullmatch(commit_sha) is None
    ):
        raise ValueError("result JSON commit SHA was invalid")
    completed_at = payload.get("completed_at")
    if (
        not isinstance(completed_at, int | float)
        or isinstance(completed_at, bool)
        or completed_at < 0
    ):
        raise ValueError("result JSON completion time was invalid")
    _validate_result_consistency(
        status=status,
        stable_code=stable_code,
        no_op=payload["no_op"],
        data_modified=payload["data_modified"],
        commit_sha=commit_sha,
    )
    evidence_codes: tuple[str, ...] = ()
    if schema_version in {2, RESULT_SCHEMA_VERSION}:
        raw_evidence = payload.get("evidence_codes")
        if type(raw_evidence) is not list or any(
            type(value) is not str or not value for value in raw_evidence
        ):
            raise ValueError("result evidence codes were invalid")
        evidence_codes = tuple(raw_evidence)
        _validate_evidence_sequence(evidence_codes)
    operation_contract_version: int | None = None
    if schema_version == RESULT_SCHEMA_VERSION:
        raw_contract_version = payload.get("operation_contract_version")
        operation_contract_version = _validate_operation_contract_version(
            raw_contract_version
        )
    _validate_persisted_contract_fields(
        schema_version=schema_version,
        operation_contract_version=operation_contract_version,
    )
    _validate_evidence_container(stable_code, evidence_codes)
    payload["evidence_codes"] = evidence_codes
    payload["operation_contract_version"] = operation_contract_version
    return payload
