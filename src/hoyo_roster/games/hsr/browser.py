"""Official-browser session bootstrap and fixed HSR read transport."""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import shlex
import stat
import subprocess
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, NoReturn
from urllib.parse import parse_qs, urlparse

from hoyo_roster.browser import BrowserContextAccess
from hoyo_roster.config import (
    resolve_private_profile_path,
)
from hoyo_roster.errors import DiagnosticProfileOverlap
from hoyo_roster.request_gate import RequestGate
from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    Request,
    Response,
    Route,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError

from .browser_launch import (
    BrowserLaunchDiagnostics as BrowserLaunchDiagnostics,
)
from .browser_launch import (
    LaunchMatrixDecision as LaunchMatrixDecision,
)
from .browser_launch import (
    _browser_executable_path as _browser_executable_path,
)
from .browser_launch import (
    _browser_exit_evidence as _browser_exit_evidence,
)
from .browser_launch import (
    _browser_log_signatures as _browser_log_signatures,
)
from .browser_launch import (
    _classify_browser_launch_error as _classify_browser_launch_error,
)
from .browser_launch import (
    _executable_architecture as _executable_architecture,
)
from .browser_launch import (
    _executable_version_check as _executable_version_check,
)
from .browser_launch import (
    _record_playwright_error as _record_playwright_error,
)
from .browser_launch import (
    decide_launch_matrix as decide_launch_matrix,
)
from .endpoint_specs import (
    AVATAR_DETAIL,
    AVATAR_LIST,
    AVATAR_LIST_BOOTSTRAP,
    AVATAR_RECOMMEND,
    BINDING,
    BUSINESS_MUTATION_PATHS,
    DEVICE_FINGERPRINT,
    DEVICE_HEADER_NAMES,
    EQUIPMENT_LIST_BOOTSTRAP,
    REQUIRED_DEVICE_HEADER_NAMES,
    VERIFY_COOKIE_TOKEN,
    EndpointSpec,
    match_registered_endpoint,
    require_registered_endpoint,
    response_has_required_fields,
)
from .errors import (
    ApiResponseError,
    AuthenticationError,
    AuthenticationRequired,
    BrowserExecutableMissing,
    BrowserLaunchError,
    BrowserLaunchTimeout,
    BrowserProfileInUse,
    BrowserProfileInvalid,
    BrowserProfilePermissionDenied,
    BrowserProfileStaleSingleton,
    ConfigurationError,
    CultivationSessionMismatch,
    RateLimitedError,
    RequestTransportError,
    RiskControlError,
    SessionInitializationError,
)
from .jsonio import StrictJsonError, loads_json

DEFAULT_BROWSER_PROFILE = Path("~/.local/share/hoyo-roster/miyoushe-profile-hsr")
LOGIN_URL = "https://www.miyoushe.com/sr/"
CULTIVATION_URL = (
    "https://act.mihoyo.com/sr/event/cultivation-tool/index.html"
    "?mhy_presentation_style=fullscreen&mhy_auth_required=true&mhy_landscape=true"
)
RISK_RETCODES = {1034, 10035}
AUTH_RETCODES = {-10001, -101, -100, 10001}
NO_RECOMMEND_RETCODES = {-502008, -502006, -502005}
_RISK_CONTROL_MARKERS = ("验证码", "设备验证", "风险验证", "captcha", "geetest")
_AUTH_COOKIE_NAMES = {"cookie_token", "cookie_token_v2"}
_MAX_INITIALIZATION_ATTEMPTS = 1
_BROWSER_LAUNCH_TIMEOUT_SECONDS = 30.0
_PAGINATION_FIELDS = {
    "total",
    "page",
    "cursor",
    "offset",
    "limit",
    "has_more",
    "page_info",
}
_SINGLETON_ARTIFACT_NAMES = ("SingletonCookie", "SingletonLock", "SingletonSocket")
_PRODUCTION_BROWSER_ARGS = (
    "--disable-background-networking",
    "--disable-component-update",
    "--no-first-run",
)


class SessionInitializationState(StrEnum):
    ACTIVE = "ACTIVE"
    TRANSPORT_READY = "TRANSPORT_READY"
    PAGE_SEALED = "PAGE_SEALED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class LoginAuthPostSpec:
    host: str
    path: str

    def matches(self, *, host: str, path: str) -> bool:
        return host == self.host and path == self.path


# Public official SDK 2026-10-12: exact host, method and path; login phase only.
LOGIN_AUTH_POST_SPECS = tuple(
    LoginAuthPostSpec(host="passport-api.mihoyo.com", path=path)
    for path in (
        "/account/ma-cn-verifier/verifier/createLoginCaptcha",
        "/account/ma-cn-passport/web/loginByMobileCaptcha",
        "/account/ma-cn-passport/web/createQRLogin",
        "/account/ma-cn-passport/web/queryQRLoginStatus",
        "/account/ma-cn-session/web/verifyCookieToken",
    )
) + (LoginAuthPostSpec(host="public-data-api.mihoyo.com", path="/device-fp/api/getFp"),)
_OFFICIAL_LOGIN_GET_SUFFIXES = (
    "hoyolab.com",
    "hoyoverse.com",
    "mihoyo.com",
    "miyoushe.com",
)


def _is_registered_login_auth_post(*, host: str, path: str) -> bool:
    return any(spec.matches(host=host, path=path) for spec in LOGIN_AUTH_POST_SPECS)


def _is_official_login_get_host(host: str) -> bool:
    return any(
        host == suffix or host.endswith(f".{suffix}") for suffix in _OFFICIAL_LOGIN_GET_SUFFIXES
    )


def contains_risk_control_text(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in _RISK_CONTROL_MARKERS)


def _singleton_artifact_names(profile: Path) -> tuple[str, ...]:
    return tuple(name for name in _SINGLETON_ARTIFACT_NAMES if os.path.lexists(profile / name))


def _raise_browser_error(
    error: BrowserLaunchError,
    diagnostics: BrowserLaunchDiagnostics,
) -> NoReturn:
    diagnostics.playwright_error_classification = error.code
    error.diagnostics = diagnostics
    raise error


def _profile_has_active_browser_root(profile: Path) -> bool:
    try:
        result = subprocess.run(
            ("ps", "-axo", "pid=,ppid=,command="),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        raise BrowserProfileInvalid("browser profile activity check failed") from None
    if result.returncode != 0:
        raise BrowserProfileInvalid("browser profile activity check failed")
    expected = profile.resolve(strict=False)
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3:
            continue
        try:
            arguments = shlex.split(fields[2])
        except ValueError:
            continue
        if any(argument == "--type" or argument.startswith("--type=") for argument in arguments):
            continue
        candidates: list[str] = []
        for index, argument in enumerate(arguments):
            if argument.startswith("--user-data-dir="):
                candidates.append(argument.split("=", 1)[1])
            elif argument == "--user-data-dir" and index + 1 < len(arguments):
                candidates.append(arguments[index + 1])
        for candidate in candidates:
            if Path(candidate).expanduser().resolve(strict=False) == expected:
                return True
    return False


def preflight_browser_profile(
    repo_root: Path,
    value: Path,
    *,
    browser_channel: Literal["chromium", "chrome"] = "chromium",
) -> tuple[Path, BrowserLaunchDiagnostics]:
    diagnostics = BrowserLaunchDiagnostics(browser_channel=browser_channel)
    root = repo_root.expanduser().resolve()
    profile = value.expanduser().absolute()
    try:
        resolve_private_profile_path(profile)
    except DiagnosticProfileOverlap as error:
        _raise_browser_error(
            BrowserProfileInvalid(str(error)),
            diagnostics,
        )
    unresolved = profile.resolve(strict=False)
    if unresolved == root or unresolved.is_relative_to(root):
        _raise_browser_error(
            BrowserProfileInvalid("browser profile category: invalid location"),
            diagnostics,
        )
    parent = profile.parent
    try:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent_metadata = parent.lstat()
    except PermissionError:
        _raise_browser_error(
            BrowserProfilePermissionDenied("browser profile category: permission denied"),
            diagnostics,
        )
    except OSError:
        _raise_browser_error(
            BrowserProfileInvalid("browser profile category: invalid parent"),
            diagnostics,
        )
    if (
        stat.S_ISLNK(parent_metadata.st_mode)
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != os.getuid()
        or parent_metadata.st_mode & 0o022
        or not os.access(parent, os.R_OK | os.W_OK | os.X_OK)
    ):
        _raise_browser_error(
            BrowserProfileInvalid("browser profile category: unsafe parent"),
            diagnostics,
        )
    diagnostics.parent_safe = True
    diagnostics.profile_exists = os.path.lexists(profile)
    if diagnostics.profile_exists:
        try:
            metadata = profile.lstat()
        except PermissionError:
            _raise_browser_error(
                BrowserProfilePermissionDenied("browser profile category: permission denied"),
                diagnostics,
            )
        diagnostics.profile_symlink = stat.S_ISLNK(metadata.st_mode)
        if diagnostics.profile_symlink or not stat.S_ISDIR(metadata.st_mode):
            _raise_browser_error(
                BrowserProfileInvalid("browser profile category: invalid directory"),
                diagnostics,
            )
    else:
        try:
            profile.mkdir(mode=0o700)
            metadata = profile.lstat()
        except PermissionError:
            _raise_browser_error(
                BrowserProfilePermissionDenied("browser profile category: permission denied"),
                diagnostics,
            )
        except OSError:
            _raise_browser_error(
                BrowserProfileInvalid("browser profile category: invalid directory"),
                diagnostics,
            )
        diagnostics.profile_exists = True
    diagnostics.profile_mode = stat.S_IMODE(metadata.st_mode)
    diagnostics.profile_owned_by_current_user = metadata.st_uid == os.getuid()
    diagnostics.profile_writable = os.access(profile, os.R_OK | os.W_OK | os.X_OK)
    if (
        not diagnostics.profile_owned_by_current_user
        or diagnostics.profile_mode != 0o700
        or not diagnostics.profile_writable
    ):
        _raise_browser_error(
            BrowserProfilePermissionDenied("browser profile category: permission denied"),
            diagnostics,
        )
    diagnostics.singleton_artifact_names = _singleton_artifact_names(profile)
    diagnostics.profile_singleton_after_failure = diagnostics.singleton_artifact_names
    try:
        diagnostics.exact_profile_active_process = _profile_has_active_browser_root(profile)
    except BrowserProfileInvalid as error:
        _raise_browser_error(error, diagnostics)
    diagnostics.active_profile_process_after_failure = diagnostics.exact_profile_active_process
    if diagnostics.exact_profile_active_process:
        _raise_browser_error(
            BrowserProfileInUse("browser profile category: in use"),
            diagnostics,
        )
    if diagnostics.singleton_artifact_names:
        _raise_browser_error(
            BrowserProfileStaleSingleton("browser profile category: singleton present"),
            diagnostics,
        )
    resolved_profile = profile.resolve()
    try:
        resolve_private_profile_path(resolved_profile)
    except DiagnosticProfileOverlap as error:
        _raise_browser_error(
            BrowserProfileInvalid(str(error)),
            diagnostics,
        )
    return resolved_profile, diagnostics


def resolve_browser_profile(repo_root: Path, value: Path) -> Path:
    profile, _ = preflight_browser_profile(repo_root, value)
    return profile


def _cookie_names(value: str) -> set[str]:
    return {
        item.split("=", 1)[0].strip()
        for item in value.split(";")
        if "=" in item and item.split("=", 1)[0].strip()
    }


def _request_body(request: Request) -> object:
    if request.post_data is None:
        return None
    try:
        return request.post_data_json
    except Exception:
        raise ConfigurationError("registered POST body was not JSON") from None


def _request_endpoint(request: Request) -> EndpointSpec:
    parsed = urlparse(request.url)
    return match_registered_endpoint(
        scheme=parsed.scheme,
        host=parsed.hostname or "",
        path=parsed.path,
        method=request.method.upper(),
        query_fields=set(parse_qs(parsed.query, keep_blank_values=True)),
        body=_request_body(request) if request.method.upper() == "POST" else None,
    )


def _response_data(
    payload: object,
    *,
    endpoint: EndpointSpec,
    allow_no_recommend: bool = False,
) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        raise ApiResponseError(f"{endpoint.name}: response root was invalid")
    retcode = payload.get("retcode")
    if isinstance(retcode, bool) or not isinstance(retcode, int):
        raise ApiResponseError(f"{endpoint.name}: retcode was invalid")
    message = payload.get("message", "")
    if isinstance(message, str) and contains_risk_control_text(message):
        raise RiskControlError(f"{endpoint.name}: risk-control response")
    if retcode in RISK_RETCODES:
        raise RiskControlError(f"{endpoint.name}: risk-control response")
    if retcode in AUTH_RETCODES:
        raise AuthenticationError(f"{endpoint.name}: login is invalid")
    if allow_no_recommend and retcode in NO_RECOMMEND_RETCODES:
        return None
    if retcode != 0:
        raise ApiResponseError(f"{endpoint.name}: API rejected request retcode={retcode}")
    if not response_has_required_fields(endpoint, payload):
        raise ApiResponseError(f"{endpoint.name}: expected response fields were missing")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ApiResponseError(f"{endpoint.name}: response data was invalid")
    return data


@dataclass(frozen=True, slots=True)
class AvatarListDiagnostics:
    query_field_names: tuple[str, ...]
    request_header_names: tuple[str, ...]
    response_root_keyset_sha256: str
    response_data_keyset_sha256: str
    entry_keyset_sha256: str
    entry_count: int
    entry_object_count: int
    is_own_present_count: int
    is_own_true_count: int
    is_own_false_count: int
    is_own_invalid_count: int
    pagination_signal_count: int
    page_route_state: str


@dataclass(frozen=True, slots=True, repr=False)
class SourceObservedRosterEvidence:
    source: Literal["AVATAR_LIST"]
    owned_ids: frozenset[str] = field(repr=False)
    diagnostics: AvatarListDiagnostics

    def __post_init__(self) -> None:
        if self.source != "AVATAR_LIST":
            raise SessionInitializationError("source-observed roster source was invalid")
        if not self.owned_ids or any(
            not identifier.isascii() or not identifier.isdigit() or identifier.startswith("0")
            for identifier in self.owned_ids
        ):
            raise SessionInitializationError("source-observed roster IDs were invalid")
        if (
            self.diagnostics.is_own_invalid_count
            or self.diagnostics.pagination_signal_count
            or self.diagnostics.is_own_true_count != len(self.owned_ids)
            or set(self.diagnostics.query_field_names) != set(AVATAR_LIST.query_fields)
            or not set(self.diagnostics.request_header_names).issuperset(
                REQUIRED_DEVICE_HEADER_NAMES
            )
            or self.diagnostics.page_route_state != SessionInitializationState.PAGE_SEALED
        ):
            raise SessionInitializationError("source-observed roster diagnostics were invalid")

    @property
    def owned_id_set_sha256(self) -> str:
        return hashlib.sha256("\n".join(sorted(self.owned_ids)).encode("utf-8")).hexdigest()

    def coverage_document(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "owned_count": len(self.owned_ids),
            "owned_id_set_sha256": self.owned_id_set_sha256,
            "entry_count": self.diagnostics.entry_count,
            "is_own_true_count": self.diagnostics.is_own_true_count,
            "is_own_false_count": self.diagnostics.is_own_false_count,
            "entry_keyset_sha256": self.diagnostics.entry_keyset_sha256,
            "response_data_keyset_sha256": (self.diagnostics.response_data_keyset_sha256),
            "pagination_signal_count": self.diagnostics.pagination_signal_count,
        }


def _pagination_signal_count(value: object) -> int:
    if isinstance(value, dict):
        return sum(key in _PAGINATION_FIELDS for key in value) + sum(
            _pagination_signal_count(child) for child in value.values()
        )
    if isinstance(value, list):
        return sum(_pagination_signal_count(child) for child in value)
    return 0


def _mapping_keyset_sha256(value: object) -> str:
    keys = sorted(value) if isinstance(value, dict) else []
    return hashlib.sha256(",".join(keys).encode("utf-8")).hexdigest()


def _entry_keyset_sha256(entries: list[object]) -> str:
    keysets = sorted(",".join(sorted(entry)) for entry in entries if isinstance(entry, dict))
    return hashlib.sha256("\n".join(keysets).encode("utf-8")).hexdigest()


def build_avatar_list_diagnostics(
    *,
    payload: object,
    query_field_names: tuple[str, ...],
    request_header_names: tuple[str, ...],
    page_route_state: str,
) -> AvatarListDiagnostics:
    data = payload.get("data") if isinstance(payload, dict) else None
    entries_value = data.get("avatars") if isinstance(data, dict) else None
    entries = entries_value if isinstance(entries_value, list) else []
    objects = [entry for entry in entries if isinstance(entry, dict)]
    is_own_values = [entry["is_own"] for entry in objects if "is_own" in entry]
    return AvatarListDiagnostics(
        query_field_names=tuple(sorted(query_field_names)),
        request_header_names=tuple(sorted(request_header_names)),
        response_root_keyset_sha256=_mapping_keyset_sha256(payload),
        response_data_keyset_sha256=_mapping_keyset_sha256(data),
        entry_keyset_sha256=_entry_keyset_sha256(entries),
        entry_count=len(entries),
        entry_object_count=len(objects),
        is_own_present_count=len(is_own_values),
        is_own_true_count=sum(value is True for value in is_own_values),
        is_own_false_count=sum(value is False for value in is_own_values),
        is_own_invalid_count=sum(type(value) is not bool for value in is_own_values),
        pagination_signal_count=_pagination_signal_count(payload),
        page_route_state=page_route_state,
    )


def build_source_observed_roster_evidence(
    *,
    owned_ids: frozenset[str],
    diagnostics: AvatarListDiagnostics,
) -> SourceObservedRosterEvidence:
    return SourceObservedRosterEvidence(
        source="AVATAR_LIST",
        owned_ids=owned_ids,
        diagnostics=diagnostics,
    )


@dataclass(slots=True)
class BrowserSession:
    playwright: Playwright | None
    context: BrowserContextAccess
    page: Page
    headed: bool
    launch_diagnostics: BrowserLaunchDiagnostics | None = field(
        default=None,
        repr=False,
    )
    request_gate: RequestGate | None = field(default=None, repr=False)
    requested_min_interval: float = 2.0
    auth_session_post_count: int = 0
    device_fingerprint_post_count: int = 0
    business_mutation_post_count: int = 0
    unregistered_post_sent_count: int = 0
    blocked_unregistered_post_count: int = 0
    blocked_business_mutation_post_count: int = 0
    blocked_other_write_count: int = 0
    blocked_registered_request_count: int = 0
    blocked_unregistered_business_get_count: int = 0
    network_request_count: int = 0
    initialization_attempts: int = 0
    initialization_state: SessionInitializationState | None = None
    auth_post_blocked_after_ready_count: int = 0
    device_post_blocked_after_ready_count: int = 0
    post_ready_external_initialization_send_count: int = 0
    post_seal_external_initialization_send_count: int = 0
    session_initialized: bool = False
    session_transport_ready: bool = False
    bootstrap_page_sealed: bool = False
    cultivation_page_opened: bool = False
    login_active: bool = False
    login_page_sealed: bool = False
    login_page_open_count: int = 0
    login_waiting_output_count: int = 0
    login_poll_count: int = 0
    business_guard_installed: bool = False
    business_guard_install_count: int = 0
    business_request_before_binding_count: int = 0
    auth_cookie_refreshed: bool = False
    verify_request_observed: bool = False
    verify_retcode: int | None = None
    device_retcode: int | None = None
    device_data_code: int | None = None
    _auth_success: bool = field(default=False, init=False, repr=False)
    _device_success: bool = field(default=False, init=False, repr=False)
    _auth_cookie_available: bool = field(default=False, init=False, repr=False)
    _initialization_error: BaseException | None = field(default=None, init=False, repr=False)
    _initialization_event: asyncio.Event = field(
        default_factory=asyncio.Event, init=False, repr=False
    )
    _allowed_requests: dict[Request, EndpointSpec] = field(
        default_factory=dict, init=False, repr=False
    )
    _initialization_admission_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False
    )
    _initialization_inflight_empty_event: asyncio.Event = field(
        default_factory=asyncio.Event, init=False, repr=False
    )
    _response_tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False, repr=False)
    _business_route_handler: Any = field(default=None, init=False, repr=False)
    _business_response_observer: Any = field(
        default=None,
        init=False,
        repr=False,
    )
    _device_headers: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _natural_session_evidence_endpoint: str | None = field(default=None, init=False, repr=False)
    _natural_request_counts: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _expected_role_uid: str | None = field(default=None, init=False, repr=False)
    _expected_role_region: str | None = field(default=None, init=False, repr=False)
    _binding_identity_verified: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if not math.isfinite(self.requested_min_interval) or self.requested_min_interval < 1.0:
            raise ConfigurationError("request interval must be at least 1 second")
        self._initialization_inflight_empty_event.set()

    async def reserve_official_action(self) -> None:
        if self.request_gate is not None:
            await self.request_gate.reserve(requested_min_interval=self.requested_min_interval)

    @property
    def natural_session_evidence_endpoint(self) -> str:
        return self._natural_session_evidence_endpoint or "NONE"

    def begin_login(self) -> None:
        if not self.headed or self.login_active or self.login_page_open_count:
            raise AuthenticationRequired("authentication category: headed login unavailable")
        if self.initialization_attempts:
            raise AuthenticationRequired("authentication category: login started after bootstrap")
        self.login_active = True
        self.login_page_sealed = False
        self.login_page_open_count = 1

    def end_login(self) -> None:
        self.login_active = False

    def bind_expected_identity(self, *, uid: str, region: str) -> None:
        if not uid.isascii() or not uid.isdigit() or uid.startswith("0") or region != "prod_gf_cn":
            raise SessionInitializationError("expected HSR identity was invalid")
        if self.initialization_attempts:
            raise SessionInitializationError("expected HSR identity was bound after bootstrap")
        if self._expected_role_uid is not None and (
            self._expected_role_uid != uid or self._expected_role_region != region
        ):
            raise SessionInitializationError("expected HSR identity changed")
        self._expected_role_uid = uid
        self._expected_role_region = region
        self._binding_identity_verified = True

    def begin_initialization(self) -> None:
        if self.initialization_attempts >= _MAX_INITIALIZATION_ATTEMPTS:
            raise SessionInitializationError("session initialization attempt limit exceeded")
        if self.login_active:
            raise SessionInitializationError("session initialization started during login")
        if self.login_page_open_count and not self.login_page_sealed:
            raise SessionInitializationError("session initialization required a sealed login page")
        if (
            self._expected_role_uid is None
            or self._expected_role_region is None
            or not self._binding_identity_verified
        ):
            raise SessionInitializationError("expected HSR identity was not bound")
        self.initialization_attempts += 1
        self.initialization_state = SessionInitializationState.ACTIVE
        self._auth_success = False
        self._device_success = False
        self._auth_cookie_available = False
        self._initialization_error = None
        self._initialization_event = asyncio.Event()
        self._device_headers = {}
        self._natural_session_evidence_endpoint = None
        self._natural_request_counts = {}
        self._allowed_requests = {}
        self._initialization_inflight_empty_event.set()
        self.session_transport_ready = False
        self.bootstrap_page_sealed = False
        self.cultivation_page_opened = False

    def record_allowed_post(self, request: Request, endpoint: EndpointSpec) -> None:
        if self.initialization_state is not SessionInitializationState.ACTIVE:
            raise SessionInitializationError("initialization POST occurred outside bootstrap")
        if endpoint.semantic_class == "AUTH_SESSION_INITIALIZATION":
            self.auth_session_post_count += 1
            self.verify_request_observed = True
        elif endpoint.semantic_class == "DEVICE_FINGERPRINT_INITIALIZATION":
            self.device_fingerprint_post_count += 1
        else:
            raise SessionInitializationError("registered POST had an invalid semantic class")
        self._allowed_requests[request] = endpoint
        self._initialization_inflight_empty_event.clear()

    def _record_blocked_after_ready(self, endpoint: EndpointSpec) -> None:
        if endpoint.semantic_class == "AUTH_SESSION_INITIALIZATION":
            self.auth_post_blocked_after_ready_count += 1
        elif endpoint.semantic_class == "DEVICE_FINGERPRINT_INITIALIZATION":
            self.device_post_blocked_after_ready_count += 1

    async def admit_initialization_request(
        self,
        route: Route,
        endpoint: EndpointSpec,
    ) -> None:
        request = route.request
        async with self._initialization_admission_lock:
            if self.initialization_state is SessionInitializationState.ACTIVE:
                try:
                    self.record_allowed_post(request, endpoint)
                    await route.continue_()
                except Exception:
                    self.complete_initialization_request(request)
                    error = SessionInitializationError(
                        "registered initialization request could not be sent"
                    )
                    self.set_initialization_error(error)
                    raise error from None
                return
            if self.initialization_state in {
                SessionInitializationState.TRANSPORT_READY,
                SessionInitializationState.PAGE_SEALED,
            }:
                self._record_blocked_after_ready(endpoint)
            self.blocked_registered_request_count += 1
            await route.abort("blockedbyclient")

    def complete_initialization_request(self, request: Request) -> None:
        self._allowed_requests.pop(request, None)
        if not self._allowed_requests:
            self._initialization_inflight_empty_event.set()

    def set_initialization_error(self, error: BaseException) -> None:
        if self._initialization_error is None:
            self._initialization_error = error
        self.initialization_state = SessionInitializationState.FAILED
        self._initialization_event.set()

    def _check_ready(self) -> None:
        auth_ready = (
            self._binding_identity_verified
            or self.auth_session_post_count == 0
            or self._auth_success
        )
        if (
            self.initialization_state is SessionInitializationState.ACTIVE
            and auth_ready
            and self._device_success
            and self._auth_cookie_available
            and self._natural_session_evidence_endpoint is not None
            and set(self._device_headers).issuperset(REQUIRED_DEVICE_HEADER_NAMES)
        ):
            self.initialization_state = SessionInitializationState.TRANSPORT_READY
            self.session_transport_ready = True
            self._initialization_event.set()

    async def wait_until_transport_ready(self, *, timeout: float = 60) -> None:
        try:
            await asyncio.wait_for(self._initialization_event.wait(), timeout=timeout)
        except TimeoutError:
            error = SessionInitializationError("official session initialization timed out")
            self.set_initialization_error(error)
            raise error from None
        await self.drain_response_tasks()
        if self._initialization_error is not None:
            raise self._initialization_error
        if (
            not self.session_transport_ready
            or self.initialization_state is not SessionInitializationState.TRANSPORT_READY
        ):
            raise SessionInitializationError("official session evidence was incomplete")

    def device_headers(self) -> dict[str, str]:
        if not self.session_transport_ready or self._initialization_error is not None:
            raise SessionInitializationError("device headers were unavailable")
        if not set(self._device_headers).issuperset(REQUIRED_DEVICE_HEADER_NAMES):
            raise SessionInitializationError("required device headers were unavailable")
        return dict(self._device_headers)

    def record_natural_session_evidence(
        self,
        *,
        endpoint: EndpointSpec,
        device_headers: Mapping[str, str],
    ) -> None:
        if self.initialization_state not in {
            SessionInitializationState.ACTIVE,
            SessionInitializationState.TRANSPORT_READY,
        }:
            raise SessionInitializationError("natural session evidence occurred outside bootstrap")
        headers = dict(device_headers)
        if not set(headers).issuperset(REQUIRED_DEVICE_HEADER_NAMES):
            raise SessionInitializationError("natural session evidence lacked device headers")
        if self._device_headers and any(
            self._device_headers[name] != headers[name] for name in REQUIRED_DEVICE_HEADER_NAMES
        ):
            raise SessionInitializationError("natural session evidence device headers changed")
        if not self._device_headers:
            self._device_headers = headers
            self._natural_session_evidence_endpoint = endpoint.name
        self._auth_cookie_available = True

    def record_natural_request_observed(self, endpoint: EndpointSpec) -> None:
        self._natural_request_counts[endpoint.name] = (
            self._natural_request_counts.get(endpoint.name, 0) + 1
        )

    def natural_request_observed_count(self, endpoint_name: str) -> int:
        return self._natural_request_counts.get(endpoint_name, 0)

    def natural_request_observed_total(self) -> int:
        return sum(self._natural_request_counts.values())

    def bind_identity(self, *, uid: str, region: str) -> None:
        if self._expected_role_uid != uid or self._expected_role_region != region:
            raise CultivationSessionMismatch("cultivation session category: role identity mismatch")

    def mark_initialized(self) -> None:
        if (
            not self.session_transport_ready
            or not self.bootstrap_page_sealed
            or self.initialization_state is not SessionInitializationState.PAGE_SEALED
        ):
            raise SessionInitializationError(
                "session initialization completed before bootstrap was sealed"
            )
        self.session_initialized = True

    def track_response_task(self, task: asyncio.Task[None]) -> None:
        self._response_tasks.add(task)
        task.add_done_callback(self._response_tasks.discard)

    async def drain_response_tasks(self) -> None:
        if self._response_tasks:
            await asyncio.gather(*tuple(self._response_tasks), return_exceptions=True)

    async def seal_bootstrap_page(self) -> None:
        if (
            not self.session_transport_ready
            or self.initialization_state is not SessionInitializationState.TRANSPORT_READY
        ):
            raise SessionInitializationError("bootstrap page sealing required session transport")
        try:
            await asyncio.wait_for(
                self._initialization_inflight_empty_event.wait(),
                timeout=60,
            )
        except TimeoutError:
            error = SessionInitializationError(
                "bootstrap page sealing timed out waiting for admitted requests"
            )
            self.set_initialization_error(error)
            raise error from None
        await self.drain_response_tasks()
        if self._initialization_error is not None:
            raise self._initialization_error
        try:
            await self.page.goto("about:blank", wait_until="domcontentloaded")
        except PlaywrightError:
            error = SessionInitializationError("official bootstrap page could not be sealed")
            self.set_initialization_error(error)
            raise error from None
        await self._initialization_inflight_empty_event.wait()
        await self.drain_response_tasks()
        if self._initialization_error is not None:
            raise self._initialization_error
        if self._allowed_requests:
            error = SessionInitializationError(
                "bootstrap page sealing left admitted requests unresolved"
            )
            self.set_initialization_error(error)
            raise error
        self.initialization_state = SessionInitializationState.PAGE_SEALED
        self.bootstrap_page_sealed = True


async def guard_login_request(
    route: Route,
    *,
    session: BrowserSession | None = None,
) -> None:
    request = route.request
    if request.url == "about:blank":
        await route.continue_()
        return
    parsed = urlparse(request.url)
    method = request.method.upper()
    if parsed.path in BUSINESS_MUTATION_PATHS:
        if session is not None and method == "POST":
            session.blocked_business_mutation_post_count += 1
        await route.abort("blockedbyclient")
        return
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        if session is not None:
            if method == "POST":
                session.blocked_unregistered_post_count += 1
            else:
                session.blocked_other_write_count += 1
        await route.abort("blockedbyclient")
        return
    if parsed.hostname == "act-api-takumi.mihoyo.com" and parsed.path.startswith(
        "/event/rpgcultivate/"
    ):
        if session is not None:
            if method == "GET":
                session.blocked_unregistered_business_get_count += 1
            elif method == "POST":
                session.blocked_unregistered_post_count += 1
            else:
                session.blocked_other_write_count += 1
        await route.abort("blockedbyclient")
        return
    if method == "GET" and _is_official_login_get_host(parsed.hostname or ""):
        await route.continue_()
        return
    if method == "POST" and _is_registered_login_auth_post(
        host=parsed.hostname or "",
        path=parsed.path,
    ):
        await route.continue_()
        return
    if session is not None:
        if method == "POST":
            session.blocked_unregistered_post_count += 1
        else:
            session.blocked_other_write_count += 1
    await route.abort("blockedbyclient")


async def guard_browser_request(session: BrowserSession, route: Route) -> None:
    request = route.request
    if request.url == "about:blank":
        await route.continue_()
        return
    session.network_request_count += 1
    parsed = urlparse(request.url)
    method = request.method.upper()
    if session.login_active:
        await guard_login_request(route, session=session)
        return
    if parsed.path in BUSINESS_MUTATION_PATHS:
        if method == "POST":
            session.blocked_business_mutation_post_count += 1
        await route.abort("blockedbyclient")
        return
    if parsed.scheme != "https":
        if method == "POST":
            session.blocked_unregistered_post_count += 1
        else:
            session.blocked_other_write_count += 1
        await route.abort("blockedbyclient")
        return
    if method == "GET":
        if parsed.hostname == "act-api-takumi.mihoyo.com" and parsed.path.startswith(
            "/event/rpgcultivate/"
        ):
            try:
                endpoint = _request_endpoint(request)
            except ConfigurationError:
                session.blocked_unregistered_business_get_count += 1
                await route.abort("blockedbyclient")
                return
            if session.initialization_state is not SessionInitializationState.ACTIVE:
                session.blocked_registered_request_count += 1
                await route.abort("blockedbyclient")
                return
            if endpoint.method != "GET":
                session.blocked_unregistered_business_get_count += 1
                await route.abort("blockedbyclient")
                return
        await route.continue_()
        return
    if method == "POST":
        try:
            endpoint = _request_endpoint(request)
        except ConfigurationError:
            session.blocked_unregistered_post_count += 1
            await route.abort("blockedbyclient")
            return
        await session.admit_initialization_request(route, endpoint)
        return
    session.blocked_other_write_count += 1
    await route.abort("blockedbyclient")


async def _observe_response(session: BrowserSession, response: Response) -> None:
    request = response.request
    endpoint = session._allowed_requests.get(request)
    if endpoint is not None:
        try:
            if request.redirected_from is not None or 300 <= response.status < 400:
                raise SessionInitializationError(
                    f"{endpoint.method} {endpoint.host}{endpoint.path}: redirect rejected"
                )
            if response.status == 429:
                raise RateLimitedError(
                    f"{endpoint.method} {endpoint.host}{endpoint.path}: HTTP 429"
                )
            if response.status < 200 or response.status >= 300:
                raise SessionInitializationError(
                    f"{endpoint.method} {endpoint.host}{endpoint.path}: HTTP failure"
                )
            payload = loads_json(await response.text())
            if isinstance(payload, dict):
                retcode = payload.get("retcode")
                if isinstance(retcode, int) and not isinstance(retcode, bool):
                    if endpoint is VERIFY_COOKIE_TOKEN:
                        session.verify_retcode = retcode
                    elif endpoint is DEVICE_FINGERPRINT:
                        session.device_retcode = retcode
                data_value = payload.get("data")
                if endpoint is DEVICE_FINGERPRINT and isinstance(data_value, dict):
                    code_value = data_value.get("code")
                    if isinstance(code_value, int) and not isinstance(code_value, bool):
                        session.device_data_code = code_value
            data = _response_data(payload, endpoint=endpoint)
            if data is None:
                raise SessionInitializationError(
                    f"{endpoint.method} {endpoint.host}{endpoint.path}: data missing"
                )
            if endpoint is DEVICE_FINGERPRINT:
                code = data.get("code")
                if isinstance(code, bool) or not isinstance(code, int) or code != 200:
                    raise SessionInitializationError(
                        f"{endpoint.method} {endpoint.host}{endpoint.path}: "
                        f"device code failed code={code}"
                    )
                session._device_success = True
            elif endpoint is VERIFY_COOKIE_TOKEN:
                session._auth_success = True
            try:
                header_array = await response.headers_array()
            except Exception:
                header_array = []
            session.auth_cookie_refreshed = session.auth_cookie_refreshed or any(
                item.get("name", "").lower() == "set-cookie" for item in header_array
            )
            session._check_ready()
        except AuthenticationError as error:
            if endpoint is VERIFY_COOKIE_TOKEN and session._binding_identity_verified:
                session._check_ready()
            else:
                session.set_initialization_error(error)
        except (
            ApiResponseError,
            RateLimitedError,
            RiskControlError,
            SessionInitializationError,
            StrictJsonError,
            UnicodeError,
            PlaywrightError,
        ) as error:
            session.set_initialization_error(error)
        finally:
            session.complete_initialization_request(request)
        return

    if request.method != "GET":
        return
    parsed = urlparse(response.url)
    try:
        endpoint = _request_endpoint(request)
    except ConfigurationError:
        return
    natural_endpoints = {
        AVATAR_LIST,
        AVATAR_LIST_BOOTSTRAP,
        EQUIPMENT_LIST_BOOTSTRAP,
    }
    if endpoint not in natural_endpoints:
        return
    session.record_natural_request_observed(endpoint)
    try:
        if request.redirected_from is not None or response.status < 200 or response.status >= 300:
            raise SessionInitializationError(
                f"{endpoint.name}: natural response HTTP status was invalid"
            )
        payload = loads_json(await response.text())
        data = _response_data(payload, endpoint=endpoint)
        if data is None:
            raise SessionInitializationError("natural response data was missing")
        headers = await request.all_headers()
        normalized_headers = {
            key.lower(): value
            for key, value in headers.items()
            if isinstance(key, str) and isinstance(value, str)
        }
        device_headers = {
            key: value
            for key, value in normalized_headers.items()
            if key in DEVICE_HEADER_NAMES and value
        }
        if not set(device_headers).issuperset(REQUIRED_DEVICE_HEADER_NAMES):
            raise SessionInitializationError("natural session lacked device headers")
        cookie_names = _cookie_names(normalized_headers.get("cookie", ""))
        if not _AUTH_COOKIE_NAMES.intersection(cookie_names):
            raise CultivationSessionMismatch(
                "cultivation session category: authentication Cookie missing"
            )
        query = parse_qs(parsed.query, keep_blank_values=True)
        if endpoint is AVATAR_LIST:
            expected = {
                "badge_region": session._expected_role_region,
                "badge_uid": session._expected_role_uid,
                "game": "hkrpg",
                "game_biz": "hkrpg_cn",
            }
        else:
            expected = {"game": "hkrpg"}
        if set(query) != set(expected) or any(
            expected[name] is None or len(query[name]) != 1 or query[name][0] != expected[name]
            for name in expected
        ):
            raise SessionInitializationError("natural request identity was invalid")
        session.record_natural_session_evidence(
            endpoint=endpoint,
            device_headers=device_headers,
        )
        if endpoint in {AVATAR_LIST, AVATAR_LIST_BOOTSTRAP}:
            entries = data.get("avatars")
        else:
            entries = data.get("list")
        if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
            raise SessionInitializationError("natural response entries were invalid")
        session._check_ready()
    except AuthenticationError as error:
        if endpoint is not AVATAR_LIST:
            session.set_initialization_error(error)
    except (
        ApiResponseError,
        RateLimitedError,
        RiskControlError,
        SessionInitializationError,
        StrictJsonError,
        UnicodeError,
        PlaywrightError,
    ) as error:
        if endpoint is AVATAR_LIST and not isinstance(error, RiskControlError):
            return
        session.set_initialization_error(error)


async def install_business_guard(
    session: BrowserSession,
    *,
    require_binding: bool = True,
) -> None:
    """Install the formal HSR Guard and observer exactly once."""

    if session.business_guard_installed or session.business_guard_install_count:
        raise SessionInitializationError("HSR business guard was already installed")
    if session.login_active:
        raise SessionInitializationError("HSR business guard installed during login")
    if require_binding and (
        session._expected_role_uid is None or session._expected_role_region is None
    ):
        raise SessionInitializationError("HSR business guard required exact Binding first")

    async def route_guard(route: Route) -> None:
        await guard_browser_request(session, route)

    def response_observer(response: Response) -> None:
        task = asyncio.create_task(_observe_response(session, response))
        session.track_response_task(task)

    await session.context.route("**/*", route_guard)
    session.context.on("response", response_observer)
    session._business_route_handler = route_guard
    session._business_response_observer = response_observer
    session.business_guard_installed = True
    session.business_guard_install_count = 1


async def remove_business_guard(session: BrowserSession) -> None:
    """Drain and remove every HSR Context-level Job hook."""

    await session.drain_response_tasks()
    route_handler = session._business_route_handler
    response_observer = session._business_response_observer
    if route_handler is not None:
        await session.context.unroute("**/*", route_handler)
    if response_observer is not None:
        session.context.remove_listener("response", response_observer)
    session._business_route_handler = None
    session._business_response_observer = None
    session.business_guard_installed = False


@asynccontextmanager
async def persistent_browser(
    *,
    repo_root: Path,
    browser_profile: Path,
    headed: bool,
    browser_channel: Literal["chromium", "chrome"] = "chromium",
    launch_check_only: bool = False,
) -> AsyncIterator[BrowserSession]:
    if browser_channel not in {"chromium", "chrome"}:
        raise ConfigurationError("browser channel was invalid")
    profile, diagnostics = preflight_browser_profile(
        repo_root,
        browser_profile,
        browser_channel=browser_channel,
    )
    playwright: Playwright | None = None
    context: BrowserContext | None = None
    session: BrowserSession | None = None
    try:
        try:
            playwright = await async_playwright().start()
        except PlaywrightError as error:
            _raise_browser_error(
                _classify_browser_launch_error(error, diagnostics),
                diagnostics,
            )
        executable = _browser_executable_path(playwright, browser_channel)
        if executable is None:
            _raise_browser_error(
                BrowserExecutableMissing("browser launch category: executable missing"),
                diagnostics,
            )
        diagnostics.executable_exists = executable.exists()
        diagnostics.executable_is_file = executable.is_file()
        diagnostics.executable_is_executable = os.access(executable, os.X_OK)
        if not (
            diagnostics.executable_exists
            and diagnostics.executable_is_file
            and diagnostics.executable_is_executable
        ):
            _raise_browser_error(
                BrowserExecutableMissing("browser launch category: executable missing"),
                diagnostics,
            )
        diagnostics.executable_version_check = _executable_version_check(executable)
        diagnostics.executable_arch = _executable_architecture(executable)
        try:
            context = await asyncio.wait_for(
                playwright.chromium.launch_persistent_context(
                    user_data_dir=str(profile),
                    headless=not headed,
                    accept_downloads=False,
                    channel="chrome" if browser_channel == "chrome" else None,
                    args=list(_PRODUCTION_BROWSER_ARGS),
                ),
                timeout=_BROWSER_LAUNCH_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            _raise_browser_error(
                BrowserLaunchTimeout("browser launch category: timeout"),
                diagnostics,
            )
        except PlaywrightError as error:
            _raise_browser_error(
                _classify_browser_launch_error(error, diagnostics),
                diagnostics,
            )
        page = context.pages[0] if context.pages else await context.new_page()
        session = BrowserSession(
            playwright,
            context,
            page,
            headed,
            launch_diagnostics=diagnostics,
        )

        if launch_check_only:

            async def route_guard(route: Route) -> None:
                session.network_request_count += 1
                await route.abort("blockedbyclient")

            await context.route("**/*", route_guard)
        else:
            await install_business_guard(session, require_binding=False)
        yield session
    finally:
        cleanup_error: PlaywrightError | None = None
        if session is not None:
            await session.drain_response_tasks()
        if context is not None:
            try:
                await context.close()
            except PlaywrightError as error:
                cleanup_error = error
        if playwright is not None:
            try:
                await playwright.stop()
            except PlaywrightError as error:
                if cleanup_error is None:
                    cleanup_error = error
        diagnostics.profile_singleton_after_failure = _singleton_artifact_names(profile)
        postcheck_error: BrowserLaunchError | None = None
        try:
            diagnostics.active_profile_process_after_failure = _profile_has_active_browser_root(
                profile
            )
        except BrowserLaunchError as error:
            postcheck_error = error
        if cleanup_error is not None:
            _raise_browser_error(
                _classify_browser_launch_error(cleanup_error, diagnostics),
                diagnostics,
            )
        if postcheck_error is not None:
            _raise_browser_error(postcheck_error, diagnostics)
        if diagnostics.active_profile_process_after_failure:
            _raise_browser_error(
                BrowserProfileInUse("browser profile category: in use after close"),
                diagnostics,
            )
        if diagnostics.profile_singleton_after_failure:
            _raise_browser_error(
                BrowserProfileStaleSingleton(
                    "browser profile category: singleton present after close"
                ),
                diagnostics,
            )


async def initialize_official_session(session: BrowserSession) -> None:
    session.begin_initialization()
    try:
        await session.reserve_official_action()
        await session.page.goto(CULTIVATION_URL, wait_until="domcontentloaded")
    except PlaywrightError:
        error = SessionInitializationError("official cultivation page could not be opened")
        session.set_initialization_error(error)
        raise error from None
    session.cultivation_page_opened = True
    await session.wait_until_transport_ready()
    await session.seal_bootstrap_page()


@dataclass(slots=True)
class OfficialCollector:
    context: BrowserContextAccess
    request_interval: float
    request_gate: RequestGate | None = field(default=None, repr=False)
    request_count: int = 0
    _last_request: float | None = field(default=None, init=False, repr=False)
    _role_uid: str | None = field(default=None, init=False, repr=False)
    _role_region: str | None = field(default=None, init=False, repr=False)
    _device_headers: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _page_route_state: str = field(default="UNAVAILABLE", init=False, repr=False)
    _avatar_list_diagnostics: AvatarListDiagnostics | None = field(
        default=None,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        minimum = 1.0 if self.request_gate is not None else 2.0
        if not math.isfinite(self.request_interval) or self.request_interval < minimum:
            raise ConfigurationError(f"request interval must be at least {minimum:g} seconds")

    async def _throttle(self) -> None:
        if self._last_request is not None:
            remaining = self.request_interval - (time.monotonic() - self._last_request)
            if remaining > 0:
                await asyncio.sleep(remaining)

    async def _reserve(self) -> None:
        if self.request_gate is not None:
            await self.request_gate.reserve(requested_min_interval=self.request_interval)
            return
        await self._throttle()

    async def reserve_navigation(self) -> None:
        await self._reserve()
        self._last_request = time.monotonic()

    def bind_role(self, *, uid: str, region: str) -> None:
        if not uid.isascii() or not uid.isdigit() or uid.startswith("0") or not region.strip():
            raise ConfigurationError("business role identity was invalid")
        if self._role_uid is not None and (self._role_uid != uid or self._role_region != region):
            raise ConfigurationError("business role identity changed during the run")
        self._role_uid = uid
        self._role_region = region

    def install_session(self, session: BrowserSession) -> None:
        if (
            not session.bootstrap_page_sealed
            or session.initialization_state is not SessionInitializationState.PAGE_SEALED
        ):
            raise SessionInitializationError("collector session required a sealed bootstrap page")
        self._device_headers = session.device_headers()
        self._page_route_state = SessionInitializationState.PAGE_SEALED.value

    def _role_params(self) -> dict[str, str]:
        if self._role_uid is None or self._role_region is None:
            raise ConfigurationError("business role identity was not bound")
        return {
            "badge_region": self._role_region,
            "badge_uid": self._role_uid,
            "game_biz": "hkrpg_cn",
        }

    async def _get(
        self,
        endpoint: EndpointSpec,
        *,
        params: Mapping[str, str],
        allow_no_recommend: bool = False,
    ) -> dict[str, Any] | None:
        registered = require_registered_endpoint(endpoint)
        if registered.method != "GET" or set(params) != set(registered.query_fields):
            raise ConfigurationError(f"{registered.name}: query contract was invalid")
        headers: dict[str, str] | None = None
        if registered.allows_device_headers:
            if not set(self._device_headers).issuperset(REQUIRED_DEVICE_HEADER_NAMES):
                raise SessionInitializationError(f"{registered.name}: session was not initialized")
            if registered.host != AVATAR_LIST.host:
                raise ConfigurationError(f"{registered.name}: device Header host was forbidden")
            headers = dict(self._device_headers)
        await self._reserve()
        try:
            response = await self.context.request.get(
                registered.url,
                params=dict(params),
                headers=headers,
                max_redirects=0,
            )
        except Exception:
            raise RequestTransportError(f"{registered.name}: request transport failed") from None
        finally:
            self._last_request = time.monotonic()
            self.request_count += 1
        if response.status == 429:
            raise RateLimitedError(f"{registered.name}: HTTP 429")
        if 300 <= response.status < 400:
            raise ApiResponseError(f"{registered.name}: redirect rejected")
        if response.status < 200 or response.status >= 300:
            raise ApiResponseError(f"{registered.name}: HTTP failure")
        try:
            payload = loads_json(await response.text())
        except (PlaywrightError, StrictJsonError, UnicodeError):
            raise ApiResponseError(f"{registered.name}: response was not JSON") from None
        if registered is AVATAR_LIST:
            self._avatar_list_diagnostics = build_avatar_list_diagnostics(
                payload=payload,
                query_field_names=tuple(params),
                request_header_names=tuple(headers or ()),
                page_route_state=self._page_route_state,
            )
        return _response_data(
            payload,
            endpoint=registered,
            allow_no_recommend=allow_no_recommend,
        )

    def avatar_list_diagnostics(self) -> AvatarListDiagnostics:
        if self._avatar_list_diagnostics is None:
            raise SessionInitializationError("avatar-list diagnostics were unavailable")
        return self._avatar_list_diagnostics

    async def binding(self) -> dict[str, Any]:
        data = await self._get(BINDING, params={"game_biz": "hkrpg_cn"})
        if data is None:
            raise ApiResponseError("binding: response data was missing")
        return data

    async def avatar_list(self) -> dict[str, Any]:
        data = await self._get(
            AVATAR_LIST,
            params={**self._role_params(), "game": "hkrpg"},
        )
        if data is None:
            raise ApiResponseError("avatar-list: response data was missing")
        return data

    async def avatar_detail(self, character_id: str) -> dict[str, Any]:
        data = await self._get(
            AVATAR_DETAIL,
            params={
                **self._role_params(),
                "change_target_level": "0",
                "game": "hkrpg",
                "item_id": character_id,
            },
        )
        if data is None:
            raise ApiResponseError("avatar-detail: response data was missing")
        return data

    async def avatar_recommend(self, character_id: str) -> dict[str, Any] | None:
        return await self._get(
            AVATAR_RECOMMEND,
            params={
                **self._role_params(),
                "game": "hkrpg",
                "is_preview": "true",
                "item_id": character_id,
            },
            allow_no_recommend=True,
        )
