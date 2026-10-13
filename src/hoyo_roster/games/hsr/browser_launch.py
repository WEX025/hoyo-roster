"""Browser executable selection and privacy-safe launch diagnostics."""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Playwright

from .errors import (
    BrowserChannelProfileIncompatible,
    BrowserExecutableMissing,
    BrowserLaunchError,
    BrowserLaunchFailedOther,
    BrowserLaunchTimeout,
    BrowserProcessExitedDuringLaunch,
    BrowserProfileInUse,
    BrowserProfileInvalid,
    BrowserProfilePermissionDenied,
    BrowserSandboxOrPlatformFailure,
)

_BROWSER_LOG_SIGNATURE_ORDER = (
    "PROCESS_EXITED",
    "TARGET_CLOSED",
    "CRASHED",
    "SEGMENTATION_FAULT",
    "ABORT_TRAP",
    "DYLD_LIBRARY_FAILURE",
    "BAD_CPU_TYPE",
    "ROSETTA_REQUIRED",
    "MACH_O_FAILURE",
    "SANDBOX_INITIALIZATION_FAILURE",
    "PROFILE_IN_USE",
    "PROFILE_VERSION_INCOMPATIBLE",
    "PROFILE_CORRUPTION",
    "PERMISSION_DENIED",
    "CRASHPAD_FAILURE",
    "HELPER_EXECUTABLE_FAILURE",
)


@dataclass(slots=True)
class BrowserLaunchDiagnostics:
    browser_channel: Literal["chromium", "chrome"]
    executable_exists: bool = False
    executable_is_file: bool = False
    executable_is_executable: bool = False
    profile_exists: bool = False
    profile_writable: bool = False
    profile_mode: int | None = None
    profile_symlink: bool = False
    profile_owned_by_current_user: bool = False
    parent_safe: bool = False
    singleton_artifact_names: tuple[str, ...] = ()
    exact_profile_active_process: bool = False
    playwright_error_classification: str | None = None
    playwright_error_digest: str = "NONE"
    browser_exit_code: int | Literal["UNKNOWN"] = "UNKNOWN"
    browser_exit_signal: str = "UNKNOWN"
    browser_log_signatures: tuple[str, ...] = ()
    executable_version_check: Literal["PASS", "FAIL"] = "FAIL"
    executable_arch: str = "UNKNOWN"
    host_arch: str = field(default_factory=lambda: platform.machine() or "UNKNOWN")
    profile_singleton_after_failure: tuple[str, ...] = ()
    active_profile_process_after_failure: bool = False


def _browser_log_signatures(raw_error: str) -> tuple[str, ...]:
    lowered = raw_error.lower()
    markers = {
        "PROCESS_EXITED": (
            "browser process exited",
            "process did exit",
            "process exited",
            "exitcode=",
        ),
        "TARGET_CLOSED": (
            "target page, context or browser has been closed",
            "browser has been closed",
        ),
        "CRASHED": ("browser crashed", "process crashed", "crashed"),
        "SEGMENTATION_FAULT": ("segmentation fault", "sigsegv"),
        "ABORT_TRAP": ("abort trap", "sigabrt"),
        "DYLD_LIBRARY_FAILURE": ("dyld:", "library not loaded"),
        "BAD_CPU_TYPE": ("bad cpu type",),
        "ROSETTA_REQUIRED": ("rosetta",),
        "MACH_O_FAILURE": ("mach-o", "mach_o"),
        "SANDBOX_INITIALIZATION_FAILURE": (
            "no usable sandbox",
            "sandbox initialization failed",
            "sandbox_init",
            "setuid sandbox",
            "suid sandbox",
        ),
        "PROFILE_IN_USE": (
            "processsingleton",
            "profile is in use",
            "profile appears to be in use",
            "user data directory is already in use",
        ),
        "PROFILE_VERSION_INCOMPATIBLE": (
            "profile version is newer",
            "incompatible profile",
            "profile is from a newer version",
        ),
        "PROFILE_CORRUPTION": ("profile is corrupted", "corrupt profile"),
        "PERMISSION_DENIED": (
            "permission denied",
            "operation not permitted",
            "eacces",
            "access is denied",
        ),
        "CRASHPAD_FAILURE": (
            "crashpad handler failed",
            "crashpad_handler: failed",
            "crashpad failed",
        ),
        "HELPER_EXECUTABLE_FAILURE": (
            "helper executable failed",
            "helper executable not found",
            "helper process failed",
        ),
    }
    found = {
        signature
        for signature, values in markers.items()
        if any(value in lowered for value in values)
    }
    return tuple(signature for signature in _BROWSER_LOG_SIGNATURE_ORDER if signature in found) or (
        "UNKNOWN",
    )


def _browser_exit_evidence(
    raw_error: str,
) -> tuple[int | Literal["UNKNOWN"], str]:
    code_match = re.search(
        r"exitCode[=:]\s*(-?\d+|null)",
        raw_error,
        re.IGNORECASE,
    )
    signal_match = re.search(
        r"(?:signal|terminationSignal)[=:]\s*([A-Za-z0-9_-]+|null)",
        raw_error,
        re.IGNORECASE,
    )
    exit_code: int | Literal["UNKNOWN"] = "UNKNOWN"
    if code_match is not None and code_match.group(1).lower() != "null":
        exit_code = int(code_match.group(1))
    exit_signal = "UNKNOWN"
    if signal_match is not None and signal_match.group(1).lower() != "null":
        exit_signal = signal_match.group(1).upper()
    return exit_code, exit_signal


def _record_playwright_error(
    diagnostics: BrowserLaunchDiagnostics,
    error: PlaywrightError,
) -> None:
    raw_error = str(error)
    diagnostics.playwright_error_digest = hashlib.sha256(raw_error.encode("utf-8")).hexdigest()
    diagnostics.browser_log_signatures = _browser_log_signatures(raw_error)
    (
        diagnostics.browser_exit_code,
        diagnostics.browser_exit_signal,
    ) = _browser_exit_evidence(raw_error)


def _browser_executable_path(
    playwright: Playwright,
    browser_channel: Literal["chromium", "chrome"],
) -> Path | None:
    if browser_channel == "chromium":
        return Path(playwright.chromium.executable_path)
    candidates: list[Path] = []
    if sys.platform == "darwin":
        candidates.append(Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    elif sys.platform.startswith("linux"):
        located = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
        if located is not None:
            candidates.append(Path(located))
    elif os.name == "nt":
        for root_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            root = os.environ.get(root_name)
            if root:
                candidates.append(Path(root) / "Google/Chrome/Application/chrome.exe")
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _executable_architecture(executable: Path) -> str:
    try:
        result = subprocess.run(
            ("file", "-b", str(executable)),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"
    lowered = result.stdout.lower()
    architectures = [
        value
        for marker, value in (
            ("arm64", "arm64"),
            ("x86_64", "x86_64"),
            ("universal", "universal"),
        )
        if marker in lowered
    ]
    return "+".join(dict.fromkeys(architectures)) if architectures else "UNKNOWN"


def _executable_version_check(executable: Path) -> Literal["PASS", "FAIL"]:
    try:
        result = subprocess.run(
            (str(executable), "--version"),
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return "FAIL"
    return "PASS" if result.returncode == 0 else "FAIL"


def _classify_browser_launch_error(
    error: PlaywrightError,
    diagnostics: BrowserLaunchDiagnostics,
) -> BrowserLaunchError:
    _record_playwright_error(diagnostics, error)
    lowered = str(error).lower()
    signatures = set(diagnostics.browser_log_signatures)
    if "executable doesn't exist" in lowered or "executable does not exist" in lowered:
        classified: BrowserLaunchError = BrowserExecutableMissing(
            "browser launch category: executable missing"
        )
    elif "PROFILE_IN_USE" in signatures:
        classified = BrowserProfileInUse("browser launch category: profile in use")
    elif "PERMISSION_DENIED" in signatures:
        classified = BrowserProfilePermissionDenied(
            "browser launch category: profile permission denied"
        )
    elif "PROFILE_VERSION_INCOMPATIBLE" in signatures:
        classified = BrowserChannelProfileIncompatible(
            "browser launch category: channel profile incompatible"
        )
    elif (
        "PROFILE_CORRUPTION" in signatures
        or "invalid user data directory" in lowered
        or "invalid profile" in lowered
    ):
        classified = BrowserProfileInvalid("browser launch category: profile invalid")
    elif "timeout" in lowered or "timed out" in lowered:
        classified = BrowserLaunchTimeout("browser launch category: timeout")
    elif (
        signatures.intersection(
            {
                "DYLD_LIBRARY_FAILURE",
                "BAD_CPU_TYPE",
                "ROSETTA_REQUIRED",
                "MACH_O_FAILURE",
                "SANDBOX_INITIALIZATION_FAILURE",
            }
        )
        or "platform is not supported" in lowered
    ):
        classified = BrowserSandboxOrPlatformFailure(
            "browser launch category: sandbox or platform failure"
        )
    elif signatures.intersection(
        {
            "PROCESS_EXITED",
            "TARGET_CLOSED",
            "CRASHED",
            "SEGMENTATION_FAULT",
            "ABORT_TRAP",
            "CRASHPAD_FAILURE",
            "HELPER_EXECUTABLE_FAILURE",
        }
    ):
        classified = BrowserProcessExitedDuringLaunch("browser launch category: process exited")
    else:
        classified = BrowserLaunchFailedOther("browser launch category: other")
    diagnostics.playwright_error_classification = classified.code
    return classified
