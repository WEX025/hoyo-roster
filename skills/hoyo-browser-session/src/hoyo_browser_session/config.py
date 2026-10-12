"""Central configuration and fixed external path layout."""

from __future__ import annotations

import math
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .errors import DiagnosticProfileOverlap

ACCOUNT_SCOPE = "primary"
PROFILE_ID = "primary"
SESSION_GAME = "session"
LOGIN_OPERATION = "login-primary"
SHARED_REQUEST_MIN_INTERVAL_SECONDS = 2.0
POST_JOB_COOLDOWN_SECONDS = 30.0
RECENT_SUCCESS_REUSE_SECONDS = 120.0
LOGIN_PROBE_INTERVAL_SECONDS = 2.0
LOGIN_TIMEOUT_SECONDS = 900.0
WORKER_HEARTBEAT_SECONDS = 5.0
WORKER_STALE_SECONDS = 30.0
SQLITE_BUSY_TIMEOUT_MS = 5000
EXECUTOR_TIMEOUT_SECONDS = 3600.0
SYNTHETIC_DELAY_SECONDS = 0.05
EVENT_HISTORY_LIMIT = 500
TERMINAL_JOB_HISTORY_LIMIT = 200
TERMINAL_RESULT_DELIVERY_GRACE_SECONDS = 300.0
PLAYWRIGHT_VERSION = "1.54.0"
PLAYWRIGHT_BROWSERS_PATH_ENV = "PLAYWRIGHT_BROWSERS_PATH"
DIAGNOSTIC_PROFILE_OVERLAP_MESSAGE = (
    "diagnostic browser profile overlaps shared Browser Owner authority"
)

DEFAULT_EXTERNAL_ROOT = Path("~/.local/share/hoyo-game-data")
SUPPORTED_GAMES = frozenset({"genshin", "zzz", "hsr", SESSION_GAME})
REAL_OPERATION = "publish-main"
SYNTHETIC_OPERATION_CONTRACT_VERSION = 1
SYNTHETIC_OPERATIONS = frozenset(
    {
        "synthetic-success",
        "synthetic-failure",
        "synthetic-rate-limit",
        "synthetic-risk-control",
        "synthetic-timeout",
    }
)


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated settings; production callers always use the fixed external root."""

    external_root: Path = DEFAULT_EXTERNAL_ROOT
    account_scope: str = ACCOUNT_SCOPE
    shared_request_min_interval_seconds: float = SHARED_REQUEST_MIN_INTERVAL_SECONDS
    post_job_cooldown_seconds: float = POST_JOB_COOLDOWN_SECONDS
    recent_success_reuse_seconds: float = RECENT_SUCCESS_REUSE_SECONDS
    login_probe_interval_seconds: float = LOGIN_PROBE_INTERVAL_SECONDS
    login_timeout_seconds: float = LOGIN_TIMEOUT_SECONDS
    worker_heartbeat_seconds: float = WORKER_HEARTBEAT_SECONDS
    worker_stale_seconds: float = WORKER_STALE_SECONDS
    terminal_result_delivery_grace_seconds: float = TERMINAL_RESULT_DELIVERY_GRACE_SECONDS
    sqlite_busy_timeout_ms: int = SQLITE_BUSY_TIMEOUT_MS
    executor_timeout_seconds: float = EXECUTOR_TIMEOUT_SECONDS
    synthetic_delay_seconds: float = SYNTHETIC_DELAY_SECONDS
    test_mode: bool = False

    def __post_init__(self) -> None:
        if self.account_scope != ACCOUNT_SCOPE:
            raise ValueError("only the primary account scope is supported")
        numeric_values = (
            self.shared_request_min_interval_seconds,
            self.post_job_cooldown_seconds,
            self.recent_success_reuse_seconds,
            self.login_probe_interval_seconds,
            self.login_timeout_seconds,
            self.worker_heartbeat_seconds,
            self.worker_stale_seconds,
            self.terminal_result_delivery_grace_seconds,
            self.executor_timeout_seconds,
            self.synthetic_delay_seconds,
        )
        if any(not math.isfinite(value) or value < 0 for value in numeric_values):
            raise ValueError("time settings must be finite non-negative values")
        if self.sqlite_busy_timeout_ms < 1:
            raise ValueError("SQLite busy timeout must be positive")
        if self.worker_stale_seconds <= self.worker_heartbeat_seconds:
            raise ValueError("worker stale timeout must exceed heartbeat interval")
        if (
            not self.test_mode
            and self.shared_request_min_interval_seconds < SHARED_REQUEST_MIN_INTERVAL_SECONDS
        ):
            raise ValueError("production request interval cannot be lowered")

    @property
    def root(self) -> Path:
        return self.external_root.expanduser().absolute()

    @property
    def runtime_root(self) -> Path:
        return self.root / "runtimes/hoyo-sync"

    @property
    def generations_root(self) -> Path:
        return self.runtime_root / "generations"

    @property
    def active_runtime_file(self) -> Path:
        return self.runtime_root / "active.json"

    @property
    def broker_root(self) -> Path:
        return self.root / "broker"

    @property
    def database_file(self) -> Path:
        return self.broker_root / "state.sqlite3"

    @property
    def worker_lock_file(self) -> Path:
        return self.broker_root / "worker.lock"

    @property
    def worker_start_lock_file(self) -> Path:
        return self.broker_root / "worker-start.lock"

    @property
    def owner_shutdown_file(self) -> Path:
        return self.broker_root / "owner-shutdown"

    @property
    def protocol_lock_file(self) -> Path:
        return self.broker_root / "protocol.lock"

    @property
    def parameter_hmac_key_file(self) -> Path:
        return self.broker_root / "parameter-hmac.key"

    @property
    def database_init_lock_file(self) -> Path:
        return self.broker_root / "db-init.lock"

    @property
    def broker_tmp(self) -> Path:
        return self.broker_root / "tmp"

    @property
    def browser_root(self) -> Path:
        return self.root / "browser"

    @property
    def profile_root(self) -> Path:
        return self.browser_root / "profiles/primary"

    @property
    def profile_user_data(self) -> Path:
        return self.profile_root / "user-data"

    @property
    def profile_lock_file(self) -> Path:
        return self.browser_root / "profile.lock"

    @property
    def profile_manifest_file(self) -> Path:
        return self.browser_root / "profile-manifest.json"

    @property
    def playwright_browsers(self) -> Path:
        return self.browser_root / "playwright-browsers"


def production_settings() -> Settings:
    return Settings()


def assert_diagnostic_profile_disjoint_from_shared_browser(
    candidate: Path,
) -> Path:
    """Reject every diagnostic path that overlaps the shared browser tree."""

    resolved_candidate = candidate.expanduser().absolute().resolve(strict=False)
    shared_browser_root = (
        production_settings().browser_root.expanduser().absolute().resolve(strict=False)
    )
    overlaps = (
        resolved_candidate == shared_browser_root
        or resolved_candidate.is_relative_to(shared_browser_root)
        or shared_browser_root.is_relative_to(resolved_candidate)
    )
    if overlaps:
        raise DiagnosticProfileOverlap(DIAGNOSTIC_PROFILE_OVERLAP_MESSAGE)
    return resolved_candidate


def configure_playwright_environment(settings: Settings) -> Path:
    """Bind Playwright lookup to the one Settings-derived browser directory."""

    path = settings.playwright_browsers
    os.environ[PLAYWRIGHT_BROWSERS_PATH_ENV] = str(path)
    return path


def playwright_subprocess_environment(
    settings: Settings,
    *,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a minimal child environment with no caller-supplied auth material."""

    source = os.environ if base is None else base
    allowed = {
        "ALL_PROXY",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "HOME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LC_ALL",
        "NO_PROXY",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SSH_AUTH_SOCK",
        "TMPDIR",
    }
    environment = {key: value for key, value in source.items() if key.upper() in allowed}
    environment[PLAYWRIGHT_BROWSERS_PATH_ENV] = str(settings.playwright_browsers)
    return environment


def isolated_test_settings(
    root: Path,
    *,
    shared_request_min_interval_seconds: float = SHARED_REQUEST_MIN_INTERVAL_SECONDS,
    post_job_cooldown_seconds: float = POST_JOB_COOLDOWN_SECONDS,
    recent_success_reuse_seconds: float = RECENT_SUCCESS_REUSE_SECONDS,
    login_probe_interval_seconds: float = LOGIN_PROBE_INTERVAL_SECONDS,
    login_timeout_seconds: float = LOGIN_TIMEOUT_SECONDS,
    worker_heartbeat_seconds: float = WORKER_HEARTBEAT_SECONDS,
    worker_stale_seconds: float = WORKER_STALE_SECONDS,
    terminal_result_delivery_grace_seconds: float = (TERMINAL_RESULT_DELIVERY_GRACE_SECONDS),
    sqlite_busy_timeout_ms: int = SQLITE_BUSY_TIMEOUT_MS,
    executor_timeout_seconds: float = EXECUTOR_TIMEOUT_SECONDS,
    synthetic_delay_seconds: float = SYNTHETIC_DELAY_SECONDS,
) -> Settings:
    """Create settings for focused tests only, below the system temporary root."""

    candidate = root.expanduser().absolute()
    temporary_root = Path(tempfile.gettempdir()).resolve()
    resolved_candidate = candidate.resolve(strict=False)
    if resolved_candidate == temporary_root or not resolved_candidate.is_relative_to(
        temporary_root
    ):
        raise ValueError("isolated test root must be below the system temporary directory")
    return Settings(
        external_root=candidate,
        shared_request_min_interval_seconds=shared_request_min_interval_seconds,
        post_job_cooldown_seconds=post_job_cooldown_seconds,
        recent_success_reuse_seconds=recent_success_reuse_seconds,
        login_probe_interval_seconds=login_probe_interval_seconds,
        login_timeout_seconds=login_timeout_seconds,
        worker_heartbeat_seconds=worker_heartbeat_seconds,
        worker_stale_seconds=worker_stale_seconds,
        terminal_result_delivery_grace_seconds=terminal_result_delivery_grace_seconds,
        sqlite_busy_timeout_ms=sqlite_busy_timeout_ms,
        executor_timeout_seconds=executor_timeout_seconds,
        synthetic_delay_seconds=synthetic_delay_seconds,
        test_mode=True,
    )
