"""Local user configuration and replaceable snapshot storage; no publication authority."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tomllib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .config import assert_diagnostic_profile_disjoint_from_shared_browser
from .repository_mutation.common import (
    atomic_write_metadata,
    copy_regular_file,
    load_metadata,
    remove_owned_tree,
    tree_digest,
)

GAMES = {
    "genshin": "genshin-impact",
    "hsr": "honkai-star-rail",
    "zzz": "zenless-zone-zero",
}
DEFAULT_CONFIG = Path("~/.config/hoyo-game-data/users.toml")
DEFAULT_DIRECT_ROOT = Path("~/.local/share/hoyo-roster")
_ALIAS = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")
_UID = re.compile(r"[1-9][0-9]{0,31}\Z")


class WorkspaceError(ValueError):
    """A fixed privacy-safe error code, never a source value."""


class SnapshotStore(Protocol):
    """The collectors own validation; storage owns installation only."""

    candidate_base: Path

    def commit(self, game_dir: Path) -> None: ...


def private_directory(path: Path) -> Path:
    """Reject symlink ancestors before creating or chmodding anything."""
    path = path.expanduser().absolute()
    for parent in (*reversed(path.parents), path):
        if parent.is_symlink():
            raise WorkspaceError("UNSAFE_PATH")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise WorkspaceError("UNSAFE_PATH")
    path.chmod(0o700)
    return path.resolve()


@dataclass(frozen=True, repr=False)
class UserConfiguration:
    user: str
    storage_root: Path
    uids: dict[str, str] = field(repr=False)

    def uid(self, game: str) -> str:
        if game not in self.uids:
            raise WorkspaceError("GAME_NOT_CONFIGURED")
        return self.uids[game]


def direct_configuration(
    game: str,
    uid: str,
    *,
    storage_root: Path = DEFAULT_DIRECT_ROOT,
) -> UserConfiguration:
    """Resolve an explicit selector without reading or writing any config file."""
    if game not in GAMES:
        raise WorkspaceError("INVALID_GAME")
    if not isinstance(uid, str) or not _UID.fullmatch(uid):
        raise WorkspaceError("INVALID_UID")
    if not storage_root.expanduser().is_absolute():
        raise WorkspaceError("INVALID_STORAGE_ROOT")
    digest = hashlib.sha256(f"direct:v1:{game}:{uid}".encode()).hexdigest()
    alias = f"direct-{game}-{digest[:48]}"
    return UserConfiguration(alias, storage_root, {game: uid})


def load_configuration(path: Path, user: str, source: Path) -> UserConfiguration:
    try:
        path = path.expanduser().absolute()
        if any(p.is_symlink() for p in (*path.parents, path)):
            raise WorkspaceError("UNSAFE_CONFIG")
        metadata = path.stat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or path.resolve().is_relative_to(source.resolve())
        ):
            raise WorkspaceError("UNSAFE_CONFIG")
        document = tomllib.loads(path.read_text(encoding="utf-8"))
        if set(document) != {"version", "storage_root", "users"} or document["version"] != 1:
            raise WorkspaceError("INVALID_CONFIG")
        if not isinstance(user, str) or not _ALIAS.fullmatch(user):
            raise WorkspaceError("INVALID_CONFIG")
        users = document["users"]
        if not isinstance(users, dict) or user not in users:
            raise WorkspaceError("USER_NOT_CONFIGURED")
        for alias, games in users.items():
            if not _ALIAS.fullmatch(alias) or not isinstance(games, dict) or not games:
                raise WorkspaceError("INVALID_CONFIG")
            for game, settings in games.items():
                if game not in GAMES or not isinstance(settings, dict) or set(settings) != {"uid"}:
                    raise WorkspaceError("INVALID_CONFIG")
                if not isinstance(settings["uid"], str) or not _UID.fullmatch(settings["uid"]):
                    raise WorkspaceError("INVALID_CONFIG")
        raw_root = document["storage_root"]
        if not isinstance(raw_root, str):
            raise WorkspaceError("INVALID_CONFIG")
        root = Path(raw_root).expanduser()
        if not root.is_absolute():
            raise WorkspaceError("INVALID_CONFIG")
        resolved = root.resolve()
        code = source.resolve()
        if resolved.is_relative_to(code) or code.is_relative_to(resolved):
            raise WorkspaceError("UNSAFE_STORAGE_ROOT")
        assert_diagnostic_profile_disjoint_from_shared_browser(resolved)
        return UserConfiguration(user, root, {g: s["uid"] for g, s in users[user].items()})
    except FileNotFoundError:
        raise WorkspaceError("CONFIG_MISSING") from None
    except WorkspaceError:
        raise
    except Exception:
        raise WorkspaceError("INVALID_CONFIG") from None


def validate_workspace(root: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(root / "tools/validate_repository.py"), "--root", str(root)],
        capture_output=True,
        check=False,
        timeout=90,
    )
    if result.returncode:
        raise WorkspaceError("WORKSPACE_VALIDATION_FAILED")


def empty_workspace(source: Path, destination: Path) -> None:
    """Copy only public contracts; never read source account data."""
    private_directory(destination)
    schemas = source / "schemas"
    tree_digest(schemas)
    shutil.copytree(schemas, destination / "schemas")
    (destination / "tools").mkdir()
    copy_regular_file(
        source / "tools/validate_repository.py", destination / "tools/validate_repository.py"
    )
    manifest: dict[str, dict[str, str]] = {}
    for game in GAMES.values():
        base = f"data/{game}"
        manifest[game] = {
            "profile": f"{base}/profile.json",
            "roster": f"{base}/roster.json",
            "builds_dir": f"{base}/builds",
        }
        game_dir = destination / base
        (game_dir / "builds").mkdir(parents=True)
        (game_dir / "builds/.gitkeep").write_bytes(b"")
        write_json(
            game_dir / "profile.json",
            {
                "schema_version": "game-profile/v1",
                "game": game,
                "account": dict.fromkeys(("uid", "server", "nickname", "player_level")),
                "sync": {"state": "not_synced", "source": None, "synced_at": None},
            },
        )
        write_json(
            game_dir / "roster.json",
            {
                "schema_version": f"{game}-roster/v1",
                "game": game,
                "characters": {},
            },
        )
        if game == "zenless-zone-zero":
            manifest[game]["bangboos"] = f"{base}/bangboos.json"
            write_json(
                game_dir / "bangboos.json",
                {
                    "schema_version": "zenless-zone-zero-bangboos/v1",
                    "game": game,
                    "items": {},
                },
            )
    write_json(
        destination / "data/manifest.json",
        {
            "schema_version": "repository-manifest/v1",
            "games": manifest,
        },
    )


def write_json(path: Path, document: object) -> None:
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


class LocalSnapshotStore:
    """One current validated generation per user, game, and UID; atomic pointer install."""

    def __init__(self, source: Path, config: UserConfiguration, game: str) -> None:
        self.source = source.resolve()
        if not _ALIAS.fullmatch(config.user) or game not in GAMES:
            raise WorkspaceError("INVALID_CONFIG")
        self.game = GAMES[game]
        self._uid = config.uid(game)
        if not _UID.fullmatch(self._uid):
            raise WorkspaceError("INVALID_CONFIG")
        resolved = config.storage_root.expanduser().resolve()
        if resolved.is_relative_to(self.source) or self.source.is_relative_to(resolved):
            raise WorkspaceError("UNSAFE_STORAGE_ROOT")
        assert_diagnostic_profile_disjoint_from_shared_browser(resolved)
        root = private_directory(config.storage_root)
        self.user_root = private_directory(root / "users" / config.user)
        key = hashlib.sha256(f"{game}:{self._uid}".encode()).hexdigest()
        self.root = private_directory(self.user_root / game / key)
        self.candidate_base = self.root / "candidates"
        # Browser state follows the same exact game/UID boundary as personal data.
        # Existing advanced-mode shared Profiles are preserved, never copied or deleted.
        self.browser_profile = self.root / "browser-profile"
        self.current_file = self.root / "current.json"
        self._locked = False

    @contextmanager
    def locked(self) -> Iterator[LocalSnapshotStore]:
        descriptor = os.open(
            self.user_root / "workspace.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        try:
            metadata = os.fstat(descriptor)
            if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
                raise WorkspaceError("UNSAFE_LOCK")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise WorkspaceError("USER_WORKSPACE_BUSY") from None
            self._locked = True
            yield self
        finally:
            self._locked = False
            os.close(descriptor)

    def current(self) -> Path | None:
        if not self.current_file.exists():
            return None
        value = load_metadata(self.current_file)
        if not isinstance(value, dict) or set(value) != {"generation"}:
            raise WorkspaceError("INVALID_CURRENT_POINTER")
        name = value["generation"]
        if not isinstance(name, str) or str(uuid.UUID(name)) != name:
            raise WorkspaceError("INVALID_CURRENT_POINTER")
        path = self.root / name
        tree_digest(path)
        if not path.is_dir():
            raise WorkspaceError("INVALID_CURRENT_POINTER")
        return path

    def prepare(self) -> Path:
        """Upgrade only public contracts, retaining this identity's current data."""
        return self._install(None)

    def commit(self, game_dir: Path) -> None:
        self._install(game_dir)

    def import_legacy(self) -> None:
        current = self.current()
        if current is None:
            raise WorkspaceError("WORKSPACE_NOT_INITIALIZED")
        profile = load_metadata(current / "data" / self.game / "profile.json")
        if profile["sync"]["state"] != "not_synced":
            raise WorkspaceError("MIGRATION_TARGET_NOT_EMPTY")
        source = self.source / "data" / self.game
        profile = load_metadata(source / "profile.json")
        if profile["account"]["uid"] != self._uid:
            raise WorkspaceError("IDENTITY_MISMATCH")
        self.commit(source)

    def _install(self, game_dir: Path | None) -> Path:
        if not self._locked:
            raise WorkspaceError("WORKSPACE_LOCK_REQUIRED")
        old = self.current()
        allowed = {"current.json", "candidates", "browser-profile"}
        if old is not None:
            allowed.add(old.name)
        if any(p.name not in allowed for p in self.root.iterdir()):
            raise WorkspaceError("WORKSPACE_RESIDUE_REQUIRES_INSPECTION")
        new = self.root / str(uuid.uuid4())
        activated = False
        try:
            empty_workspace(self.source, new)
            existing = (
                game_dir if game_dir is not None else (old / "data" / self.game if old else None)
            )
            if existing is not None:
                tree_digest(existing)
                account = load_metadata(existing / "profile.json")
                uid = account["account"]["uid"]
                if uid is not None and uid != self._uid:
                    raise WorkspaceError("IDENTITY_MISMATCH")
                target = new / "data" / self.game
                remove_owned_tree(target)
                shutil.copytree(existing, target)
            validate_workspace(new)
            for p in new.rglob("*"):
                p.chmod(0o700 if p.is_dir() else 0o600)
                if p.is_file():
                    with p.open("rb") as stream:
                        os.fsync(stream.fileno())
            for directory in (*reversed([p for p in new.rglob("*") if p.is_dir()]), new):
                descriptor = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            atomic_write_metadata(self.current_file, {"generation": new.name})
            activated = True
            if old is not None:
                remove_owned_tree(old)
            return new
        finally:
            if not activated and new.exists():
                remove_owned_tree(new)
