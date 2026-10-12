"""Fixed shared persistent Chromium Profile lifecycle and safe preflight."""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import sqlite3
import stat
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

from .config import ACCOUNT_SCOPE, PROFILE_ID, Settings
from .db import Database
from .errors import (
    BrokerError,
    ExternalStateUnsafe,
    ProfileBroken,
    ProfileInUse,
    ProfileStaleSingleton,
)
from .locks import (
    FileLock,
    ensure_private_directory,
    ensure_private_file,
    ensure_safe_parent_directory,
    is_lock_held,
)
from .models import StableCode
from .operational_events import (
    OperationalEvent,
    OperationalEventStore,
    OperationalEventType,
)

_SINGLETON_NAMES = ("SingletonCookie", "SingletonLock", "SingletonSocket")
ProcessScanner = Callable[[Path], bool]


def _command_uses_profile(command: str, profile: Path) -> bool:
    try:
        arguments = shlex.split(command)
    except ValueError:
        return False
    expected = profile.expanduser().resolve(strict=False)
    for index, argument in enumerate(arguments):
        value: str | None = None
        if argument.startswith("--user-data-dir="):
            value = argument.partition("=")[2]
        elif argument == "--user-data-dir" and index + 1 < len(arguments):
            value = arguments[index + 1]
        if value:
            try:
                candidate = Path(value).expanduser().resolve(strict=False)
            except OSError:
                continue
            if candidate == expected:
                return True
    return False


def _default_process_scanner(profile: Path) -> bool:
    """Return whether a process command line names this exact User Data Directory."""

    try:
        result = subprocess.run(
            ["ps", "-axo", "command="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return True
    if result.returncode != 0:
        return True
    return any(_command_uses_profile(line, profile) for line in result.stdout.splitlines())


class ProfileManager:
    """Own the one fixed physical Profile without exposing its path in repr."""

    def __init__(
        self,
        settings: Settings,
        *,
        process_scanner: ProcessScanner = _default_process_scanner,
    ) -> None:
        self.settings = settings
        self._process_scanner = process_scanner
        self._database = Database(settings)
        self._events = OperationalEventStore(settings, self._database)

    def __repr__(self) -> str:
        return f"ProfileManager(profile_id={PROFILE_ID!r})"

    @property
    def user_data_dir(self) -> Path:
        return self.settings.profile_user_data

    def initialize(self, *, repo_root: Path | None = None) -> None:
        self._database.initialize()
        try:
            ensure_safe_parent_directory(self.settings.root)
            ensure_private_directory(self.settings.browser_root)
            ensure_private_directory(self.settings.browser_root / "profiles")
            ensure_private_directory(self.settings.profile_root)
            profile = ensure_private_directory(self.settings.profile_user_data)
            ensure_private_directory(self.settings.playwright_browsers)
            ensure_private_file(self.settings.profile_lock_file)
            self._assert_outside_repository(profile, repo_root)
            self._ensure_manifest()
        except ExternalStateUnsafe as error:
            self._raise_profile_broken(
                "Profile storage path is unsafe",
                cause=error,
            )
        try:
            with self._database.immediate() as connection:
                connection.execute(
                    """
                    INSERT INTO profile_state(
                        profile_id, account_scope, browser_channel,
                        state, expected_account_hash, last_verified_at, updated_at
                    ) VALUES(?, ?, 'chromium', 'READY', NULL, NULL, ?)
                    ON CONFLICT(profile_id) DO UPDATE SET
                        account_scope = excluded.account_scope,
                        browser_channel = excluded.browser_channel,
                        updated_at = excluded.updated_at
                    """,
                    (PROFILE_ID, ACCOUNT_SCOPE, time.time()),
                )
        except sqlite3.Error as error:
            self._raise_profile_broken(
                "Profile state could not be initialized",
                cause=error,
            )

    def preflight(
        self,
        *,
        repo_root: Path | None = None,
        check_lock: bool = True,
    ) -> None:
        self.initialize(repo_root=repo_root)
        profile = self._validated_profile()
        if check_lock:
            try:
                locked = is_lock_held(self.settings.profile_lock_file)
            except ExternalStateUnsafe as error:
                self._raise_profile_broken(
                    "Profile lock state is unsafe",
                    cause=error,
                )
            if locked:
                raise ProfileInUse("shared browser Profile is locked")
        self._inspect_occupancy(profile)

    def postflight(self) -> None:
        """Inspect the exact Profile after close without changing any state."""

        profile = self._validated_profile()
        self._inspect_occupancy(profile)

    def _validated_profile(self) -> Path:
        path = self.settings.profile_user_data
        try:
            metadata = path.lstat()
        except OSError as error:
            self._raise_profile_broken(
                "Profile is unavailable",
                cause=error,
            )
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            self._raise_profile_broken("Profile ownership, type, or permissions are unsafe")
        try:
            return path.resolve(strict=True)
        except OSError as error:
            self._raise_profile_broken(
                "Profile path could not be resolved",
                cause=error,
            )

    def _inspect_occupancy(self, profile: Path) -> None:
        if self._process_scanner(profile):
            raise ProfileInUse("shared browser Profile has an active root process")
        if any(os.path.lexists(profile / name) for name in _SINGLETON_NAMES):
            raise ProfileStaleSingleton("shared browser Profile has stale Chromium Singleton state")

    def _acquire_owner_lock(self, *, repo_root: Path | None = None) -> FileLock:
        """Acquire the primary Profile lock for the private Owner lifecycle."""

        self.preflight(repo_root=repo_root)
        lock = FileLock(self.settings.profile_lock_file)
        try:
            acquired = lock.acquire(blocking=False)
        except ExternalStateUnsafe as error:
            self._raise_profile_broken(
                "Profile lock state is unsafe",
                cause=error,
            )
        if not acquired:
            raise ProfileInUse("shared browser Profile is locked")
        try:
            self.preflight(repo_root=repo_root, check_lock=False)
        except BaseException:
            lock.release()
            raise
        return lock

    def _assert_outside_repository(
        self,
        profile: Path,
        repo_root: Path | None,
    ) -> None:
        if repo_root is None:
            return
        root = repo_root.expanduser().resolve(strict=True)
        if profile == root or profile.is_relative_to(root):
            self._raise_profile_broken("Profile must remain outside the repository")

    def _ensure_manifest(self) -> None:
        path = self.settings.profile_manifest_file
        if os.path.lexists(path):
            safe = ensure_private_file(path)
            try:
                payload = json.loads(safe.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                self._raise_profile_broken(
                    "Profile manifest is invalid",
                    cause=error,
                )
            if payload != {
                "schema_version": 1,
                "profile_id": PROFILE_ID,
                "account_scope": ACCOUNT_SCOPE,
                "browser_channel": "chromium",
            }:
                self._raise_profile_broken("Profile manifest fields are invalid")
            return
        payload = {
            "account_scope": ACCOUNT_SCOPE,
            "browser_channel": "chromium",
            "profile_id": PROFILE_ID,
            "schema_version": 1,
        }
        temporary = path.with_name(f".{path.name}.tmp")
        if os.path.lexists(temporary):
            self._raise_profile_broken("Profile manifest temporary residue exists")
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError as error:
            with contextlib.suppress(OSError):
                if os.path.lexists(temporary):
                    os.unlink(temporary)
            self._raise_profile_broken(
                "Profile manifest could not be initialized",
                cause=error,
            )
        ensure_private_file(path)

    def _record_profile_broken(self) -> None:
        try:
            self._database.initialize()
            recorded_at = time.time()
            with self._database.immediate() as connection:
                connection.execute(
                    """
                    UPDATE account_state
                    SET state = 'PROFILE_BROKEN', next_job_at = 0,
                        blocked_reason = 'PROFILE_BROKEN', updated_at = ?
                    WHERE account_scope = ?
                    """,
                    (recorded_at, ACCOUNT_SCOPE),
                )
                connection.execute(
                    """
                    INSERT INTO profile_state(
                        profile_id, account_scope, browser_channel, state,
                        expected_account_hash, last_verified_at, updated_at
                    ) VALUES(?, ?, 'chromium', 'BROKEN', NULL, NULL, ?)
                    ON CONFLICT(profile_id) DO UPDATE SET
                        account_scope = excluded.account_scope,
                        browser_channel = excluded.browser_channel,
                        state = excluded.state,
                        updated_at = excluded.updated_at
                    """,
                    (PROFILE_ID, ACCOUNT_SCOPE, recorded_at),
                )
                self._events.record_in_transaction(
                    connection,
                    OperationalEvent(
                        occurred_at=recorded_at,
                        event_type=OperationalEventType.PROFILE_BROKEN,
                        game=None,
                        operation=None,
                        job_id=None,
                        stable_code=StableCode.PROFILE_BROKEN,
                        diagnostic_hash=None,
                    ),
                )
        except (OSError, sqlite3.Error, BrokerError):
            return

    def _raise_profile_broken(
        self,
        message: str,
        *,
        cause: BaseException | None = None,
    ) -> NoReturn:
        self._record_profile_broken()
        error = ProfileBroken(message)
        if cause is None:
            raise error
        raise error from cause
