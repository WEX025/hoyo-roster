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
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .storage.common import (
    AtomicWriteError,
    MetadataError,
    secure_private_directory,
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
    """Create every new component privately; never chmod existing user directories."""
    try:
        return secure_private_directory(path)
    except MetadataError:
        raise WorkspaceError("UNSAFE_PRIVATE_DIRECTORY") from None


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


def validate_workspace(root: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parent / "contracts/tools/validate_repository.py"),
            "--root",
            str(root),
        ],
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
        for parent in (self.source, *self.source.parents):
            if (parent / ".git").exists():
                candidate = config.storage_root.expanduser().resolve()
                if candidate.is_relative_to(parent) or parent.is_relative_to(candidate):
                    raise WorkspaceError("UNSAFE_STORAGE_ROOT")
                break
        if not _ALIAS.fullmatch(config.user) or game not in GAMES:
            raise WorkspaceError("INVALID_CONFIG")
        self.game = GAMES[game]
        self._uid = config.uid(game)
        if not _UID.fullmatch(self._uid):
            raise WorkspaceError("INVALID_CONFIG")
        resolved = config.storage_root.expanduser().resolve()
        if resolved.is_relative_to(self.source) or self.source.is_relative_to(resolved):
            raise WorkspaceError("UNSAFE_STORAGE_ROOT")
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
        self.commit_state = "NOT_COMMITTED"
        self.cleanup_pending = False
        self.transaction_file = self.root / "transaction.json"

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

    @property
    def scope_digest(self) -> str:
        return hashlib.sha256(f"{self.game}:{self._uid}".encode()).hexdigest()

    @property
    def candidate_region(self) -> str | None:
        """Use the validated current region when present, without inferring one for a baseline."""
        try:
            current = self.current()
        except WorkspaceError:
            return None
        if current is None:
            return None
        return load_metadata(current / "data" / self.game / "profile.json")["account"]["server"]

    def _generation(self, name: object) -> Path:
        if not isinstance(name, str):
            raise WorkspaceError("INVALID_GENERATION")
        try:
            if str(uuid.UUID(name)) != name:
                raise ValueError
        except ValueError:
            raise WorkspaceError("INVALID_GENERATION") from None
        return self.root / name

    def _verify_generation(self, path: Path) -> None:
        tree_digest(path)
        if not path.is_dir():
            raise WorkspaceError("INVALID_CURRENT_POINTER")
        for item in (path, *path.rglob("*")):
            metadata = item.lstat()
            if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != (
                0o700 if item.is_dir() else 0o600
            ):
                raise WorkspaceError("UNSAFE_GENERATION")
        marker = load_metadata(path / "generation.json")
        expected = {
            "authority": "hoyo-roster/generation-v1",
            "scope": self.scope_digest,
            "generation": path.name,
            "data_digest": tree_digest(path / "data"),
            "contracts_digest": hashlib.sha256(
                (tree_digest(path / "schemas") + tree_digest(path / "tools")).encode()
            ).hexdigest(),
        }
        if marker != expected:
            raise WorkspaceError("GENERATION_OWNERSHIP_UNPROVEN")
        profile = load_metadata(path / "data" / self.game / "profile.json")
        if profile["account"]["uid"] not in (None, self._uid):
            raise WorkspaceError("IDENTITY_MISMATCH")
        for game in GAMES.values():
            if game != self.game:
                other = load_metadata(path / "data" / game / "profile.json")
                if other["sync"]["state"] != "not_synced":
                    raise WorkspaceError("IDENTITY_SCOPE_CONFLICT")
        validate_workspace(path)

    def current(self) -> Path | None:
        if not os.path.lexists(self.current_file):
            return None
        try:
            self._verify_private_metadata(self.current_file)
            value = load_metadata(self.current_file)
            if not isinstance(value, dict) or set(value) != {"generation"}:
                raise ValueError
            path = self._generation(value["generation"])
            self._verify_generation(path)
            return path
        except (MetadataError, ValueError, OSError, KeyError):
            raise WorkspaceError("INVALID_CURRENT_POINTER") from None

    def inspect(self) -> dict[str, object]:
        try:
            current = self.current()
            state = "PRESENT" if current else "EMPTY"
        except WorkspaceError:
            current = None
            state = "INVALID"
        generations = []
        for item in sorted(self.root.iterdir()):
            try:
                self._generation(item.name)
            except WorkspaceError:
                continue
            try:
                self._verify_generation(item)
                status = "VERIFIED"
            except Exception:
                status = "UNVERIFIED"
            generations.append(
                {"generation": item.name, "status": status, "current": item == current}
            )
        return {
            "current": state,
            "generations": generations,
            "recovery_required": os.path.lexists(self.transaction_file),
        }

    def prepare(self) -> Path:
        return self._install(None)

    def commit(self, game_dir: Path) -> None:
        self._install(game_dir)

    def logout(self) -> None:
        self._require_lock()
        if self.browser_profile.exists():
            metadata = self.browser_profile.lstat()
            if (
                self.browser_profile.is_symlink()
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise WorkspaceError("UNSAFE_BROWSER_PROFILE")
            # Browser must be closed; never remove live or stale singleton authority.
            if any(
                (self.browser_profile / name).is_symlink() or (self.browser_profile / name).exists()
                for name in ("SingletonLock", "SingletonSocket", "SingletonCookie")
            ):
                raise WorkspaceError("BROWSER_PROFILE_IN_USE_OR_STALE")
            remove_owned_tree(self.browser_profile)
        for path in (self.root / "identity.key", self.root / "verified-identity.json"):
            if os.path.lexists(path):
                metadata = path.lstat()
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                    raise WorkspaceError("UNSAFE_IDENTITY_STATE")
                path.unlink()

    def _verify_private_metadata(self, path: Path) -> None:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise WorkspaceError("UNSAFE_METADATA")

    def _require_lock(self) -> None:
        if not self._locked:
            raise WorkspaceError("WORKSPACE_LOCK_REQUIRED")

    def _sync_pointer_directory(self) -> None:
        descriptor = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _inventory(self, path: Path) -> dict[str, str]:
        tree_digest(path)
        return {
            item.relative_to(path).as_posix(): (
                "directory" if item.is_dir() else hashlib.sha256(item.read_bytes()).hexdigest()
            )
            for item in sorted(path.rglob("*"))
        }

    def _verify_cleanup_inventory(self, path: Path, inventory: object) -> None:
        # A partially deleted old generation cannot pass a full Validator again.
        # The durable journal records its previously validated exact file hashes.
        if not isinstance(inventory, dict) or not inventory:
            raise WorkspaceError("UNOWNED_CLEANUP_RESIDUE")
        for name, digest in inventory.items():
            relative = Path(name)
            if (
                not isinstance(name, str)
                or relative.is_absolute()
                or ".." in relative.parts
                or not relative.parts
            ):
                raise WorkspaceError("UNOWNED_CLEANUP_RESIDUE")
            if not isinstance(digest, str) or (
                digest != "directory" and re.fullmatch("[0-9a-f]{64}", digest) is None
            ):
                raise WorkspaceError("UNOWNED_CLEANUP_RESIDUE")
        if not set(self._inventory(path).items()).issubset(set(inventory.items())):
            raise WorkspaceError("CLEANUP_RESIDUE_CHANGED")
        for item in (path, *path.rglob("*")):
            metadata = item.lstat()
            if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != (
                0o700 if item.is_dir() else 0o600
            ):
                raise WorkspaceError("UNSAFE_CLEANUP_RESIDUE")

    def cleanup_generation(self, generation: str) -> None:
        self._require_lock()
        current = self.current()
        if current is None:
            raise WorkspaceError("RECOVERY_REQUIRED")
        if os.path.lexists(self.transaction_file):
            raise WorkspaceError("RECOVERY_REQUIRED")
        target = self._generation(generation)
        if target == current:
            raise WorkspaceError("CURRENT_GENERATION_PROTECTED")
        self._verify_generation(target)
        remove_owned_tree(target)
        self._sync_pointer_directory()

    def recover(self, generation: str | None = None) -> Path:
        """Reconcile only scope-owned, fully validated generations under the lock."""
        self._require_lock()
        try:
            current = self.current()
        except WorkspaceError:
            current = None
            if generation is None:
                raise WorkspaceError("RECOVERY_GENERATION_REQUIRED") from None
        if generation is not None and os.path.lexists(self.transaction_file):
            self._verify_private_metadata(self.transaction_file)
            journal = load_metadata(self.transaction_file)
            if (
                not isinstance(journal, dict)
                or set(journal) != {"authority", "scope", "old", "new", "inventories"}
                or journal.get("authority") != "hoyo-roster/transaction-v1"
                or journal.get("scope") != self.scope_digest
                or generation not in (journal.get("old"), journal.get("new"))
            ):
                raise WorkspaceError("RECOVERY_REFERENCE_CONFLICT")
        if generation is not None:
            target = self._generation(generation)
            self._verify_generation(target)
            atomic_write_metadata(self.current_file, {"generation": target.name})
            current = target
        if current is None:
            raise WorkspaceError("WORKSPACE_NOT_INITIALIZED")
        # Confirm persistence before deleting any possibly referenced data.
        self._sync_pointer_directory()
        self.commit_state = "COMMITTED"
        if os.path.lexists(self.transaction_file):
            self._verify_private_metadata(self.transaction_file)
            value = load_metadata(self.transaction_file)
            if (
                not isinstance(value, dict)
                or set(value) != {"authority", "scope", "old", "new", "inventories"}
                or value["authority"] != "hoyo-roster/transaction-v1"
                or value["scope"] != self.scope_digest
            ):
                raise WorkspaceError("UNOWNED_TRANSACTION")
            targets = [self._generation(n) for n in (value["old"], value["new"]) if n is not None]
            if current not in targets:
                raise WorkspaceError("RECOVERY_REFERENCE_CONFLICT")
            inventories = value["inventories"]
            if not isinstance(inventories, dict) or set(inventories) != {
                target.name for target in targets
            }:
                raise WorkspaceError("UNOWNED_TRANSACTION")
            for target in targets:
                if target != current and os.path.lexists(target):
                    self._verify_cleanup_inventory(target, inventories[target.name])
                    try:
                        remove_owned_tree(target)
                    except (OSError, MetadataError):
                        self.cleanup_pending = True
                        return current
            self.transaction_file.unlink()
            self._sync_pointer_directory()
        self.cleanup_pending = False
        return current

    def _install(self, game_dir: Path | None) -> Path:
        self._require_lock()
        if os.path.lexists(self.transaction_file):
            raise WorkspaceError("RECOVERY_REQUIRED")
        self.commit_state = "NOT_COMMITTED"
        old = self.current()
        allowed = {
            "current.json",
            "candidates",
            "browser-profile",
            "request-state.json",
            "identity.key",
            "verified-identity.json",
        }
        if old:
            allowed.add(old.name)
        if any(p.name not in allowed for p in self.root.iterdir()):
            raise WorkspaceError("WORKSPACE_RESIDUE_REQUIRES_INSPECTION")
        new = self.root / str(uuid.uuid4())
        journal_attempted = False
        self.commit_state = "PRE_COMMIT"
        try:
            empty_workspace(self.source, new)
            existing = (
                game_dir if game_dir is not None else (old / "data" / self.game if old else None)
            )
            if existing is not None:
                tree_digest(existing)
                account = load_metadata(existing / "profile.json")
                if account["account"]["uid"] not in (None, self._uid):
                    raise WorkspaceError("IDENTITY_MISMATCH")
                target = new / "data" / self.game
                remove_owned_tree(target)
                shutil.copytree(existing, target)
            validate_workspace(new)
            write_json(
                new / "generation.json",
                {
                    "authority": "hoyo-roster/generation-v1",
                    "scope": self.scope_digest,
                    "generation": new.name,
                    "data_digest": tree_digest(new / "data"),
                    "contracts_digest": hashlib.sha256(
                        (tree_digest(new / "schemas") + tree_digest(new / "tools")).encode()
                    ).hexdigest(),
                },
            )
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
            self._verify_generation(new)
            inventories = {
                path.name: self._inventory(path) for path in (old, new) if path is not None
            }
            journal_attempted = True
            atomic_write_metadata(
                self.transaction_file,
                {
                    "authority": "hoyo-roster/transaction-v1",
                    "scope": self.scope_digest,
                    "old": old.name if old else None,
                    "new": new.name,
                    "inventories": inventories,
                },
            )
            try:
                atomic_write_metadata(self.current_file, {"generation": new.name})
            except AtomicWriteError:
                self.commit_state = "DURABILITY_UNKNOWN"
                raise WorkspaceError("COMMIT_DURABILITY_UNKNOWN") from None
            self.commit_state = "COMMITTED"
            try:
                self.recover()
            except (OSError, MetadataError):
                self.cleanup_pending = True
            return new
        except Exception:
            if self.commit_state == "PRE_COMMIT":
                self.commit_state = "NOT_COMMITTED"
            raise
        finally:
            # Once a journal may exist, retain both generations for verified recovery.
            if not journal_attempted and new.exists():
                remove_owned_tree(new)
