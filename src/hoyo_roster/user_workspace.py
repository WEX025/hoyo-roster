"""Local user configuration and replaceable snapshot storage; no publication authority."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import paths
from . import platform_io as io
import portalocker
from .storage.candidate import CandidateScope, REGIONS
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
    auth_root: Path = field(default_factory=paths.default_private_root)

    def uid(self, game: str) -> str:
        if game not in self.uids:
            raise WorkspaceError("GAME_NOT_CONFIGURED")
        return self.uids[game]


def direct_configuration(
    game: str,
    uid: str,
    *,
    storage_root: Path | None = None,
    auth_root: Path | None = None,
) -> UserConfiguration:
    """Resolve an explicit selector without reading or writing any config file."""
    if game not in GAMES:
        raise WorkspaceError("INVALID_GAME")
    if not isinstance(uid, str) or not _UID.fullmatch(uid):
        raise WorkspaceError("INVALID_UID")
    storage_root = paths.default_storage_root() if storage_root is None else storage_root
    auth_root = paths.default_private_root() if auth_root is None else auth_root
    if not storage_root.expanduser().is_absolute():
        raise WorkspaceError("INVALID_STORAGE_ROOT")
    if not auth_root.expanduser().is_absolute():
        raise WorkspaceError("INVALID_AUTH_ROOT")
    digest = hashlib.sha256(f"direct:v1:{game}:{uid}".encode()).hexdigest()
    alias = f"direct-{game}-{digest[:48]}"
    return UserConfiguration(alias, storage_root, {game: uid}, auth_root)


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
    io.set_private(path, 0o600)


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
        try:
            io.check_components(config.storage_root)
            io.check_components(config.auth_root)
        except (io.UnsafePath, OSError):
            raise WorkspaceError("UNSAFE_PRIVATE_DIRECTORY") from None
        resolved = config.storage_root.expanduser().resolve()
        if resolved.is_relative_to(self.source) or self.source.is_relative_to(resolved):
            raise WorkspaceError("UNSAFE_STORAGE_ROOT")
        repository = paths.project_repository()
        owner = paths.containing_repository(resolved)
        repository_data = repository / "data" if repository else None
        if owner is not None and (
            repository_data is None or not resolved.is_relative_to(repository_data)
        ):
            raise WorkspaceError("UNSAFE_STORAGE_ROOT")
        if repository is not None and repository.is_relative_to(resolved):
            raise WorkspaceError("UNSAFE_STORAGE_ROOT")
        auth = config.auth_root.expanduser().resolve()
        if (
            paths.containing_repository(auth) is not None
            or auth.is_relative_to(self.source)
            or self.source.is_relative_to(auth)
            or (repository is not None and repository.is_relative_to(auth))
        ):
            raise WorkspaceError("UNSAFE_AUTH_ROOT")
        # A cloned data/ container is usually 0755 because of its tracked placeholder.
        # Leave it unchanged; all identity directories below it must still be 0700.
        if resolved == repository_data and config.storage_root.exists():
            if not io.repository_container_safe(config.storage_root):
                raise WorkspaceError("UNSAFE_PRIVATE_DIRECTORY")
            root = config.storage_root
        else:
            root = private_directory(config.storage_root)
        self.user_root = private_directory(root / "users" / config.user)
        key = hashlib.sha256(f"{game}:{self._uid}".encode()).hexdigest()
        self.root = private_directory(self.user_root / game / key)
        self.candidate_base = self.root / "candidates"
        auth = private_directory(config.auth_root)
        self.auth_user_root = private_directory(auth / "users" / config.user)
        self.auth_root = private_directory(self.auth_user_root / game / key)
        if owner is not None and any(
            os.path.lexists(self.root / name)
            for name in ("browser-profile", "identity.key", "verified-identity.json")
        ):
            raise WorkspaceError("AUTH_STATE_IN_RESULT_DIRECTORY")
        # Authentication is independent of the result destination; never migrate user data.
        self.browser_profile = self.auth_root / "browser-profile"
        self.current_file = self.root / "current.json"
        self._locked = False
        self.commit_state = "NOT_COMMITTED"
        self.cleanup_pending = False
        self.transaction_file = self.root / "transaction.json"
        self.identity_file = self.root / "workspace-identity.json"

    @contextmanager
    def locked(self) -> Iterator[LocalSnapshotStore]:
        # Serialize both result writes and auth/session reuse across result destinations.
        with ExitStack() as stack:
            for root in sorted({self.user_root, self.auth_user_root}):
                stack.enter_context(self._lock_directory(root))
            self._locked = True
            try:
                yield self
            finally:
                self._locked = False

    @contextmanager
    def _lock_directory(self, root: Path) -> Iterator[None]:
        try:
            with io.exclusive_lock(root / "workspace.lock"):
                yield
        except portalocker.exceptions.LockException:
            raise WorkspaceError("USER_WORKSPACE_BUSY") from None
        except io.UnsafePath:
            raise WorkspaceError("UNSAFE_LOCK") from None

    @property
    def scope_digest(self) -> str:
        return hashlib.sha256(f"{self.game}:{self._uid}".encode()).hexdigest()

    @property
    def candidate_region(self) -> str | None:
        """Use durable identity authority, including when the current pointer is corrupt."""
        region = self._identity_region()
        # An unbound baseline is not authority to ignore contradictory saved data.
        # Incomplete cleanup must be recovered before separate candidate cleanup.
        found = False
        for item in self.root.iterdir():
            try:
                self._generation(item.name)
            except WorkspaceError:
                continue
            self._verify_generation(item)
            found = True
        if not found:
            raise WorkspaceError("IDENTITY_BOUNDARY_UNPROVEN")
        return region

    def _identity_document(self, region: str | None) -> dict[str, object]:
        return {
            "authority": "hoyo-roster/workspace-identity-v1",
            "scope": self.scope_digest,
            "game": self.game,
            "uid_digest": CandidateScope.identity(self.game, self._uid).uid_digest,
            "region": region,
        }

    def _identity_region(self) -> str | None:
        try:
            self._verify_private_metadata(self.identity_file)
            value = load_metadata(self.identity_file)
            region = value["region"]
            if value != self._identity_document(region) or (
                region is not None and region not in REGIONS[self.game]
            ):
                raise ValueError
            return region
        except (MetadataError, OSError, ValueError, KeyError, TypeError):
            raise WorkspaceError("IDENTITY_BOUNDARY_UNPROVEN") from None

    def _profile_region(self, profile: object, region: str | None) -> str | None:
        try:
            if profile["game"] != self.game:
                raise ValueError
            account = profile["account"]
            actual = account["server"]
            if profile["sync"]["state"] == "not_synced":
                if account["uid"] is not None or actual is not None:
                    raise ValueError
            elif (
                account["uid"] != self._uid
                or actual not in REGIONS[self.game]
                or (region is not None and actual != region)
            ):
                raise ValueError
            return actual
        except (ValueError, KeyError, TypeError):
            raise WorkspaceError("IDENTITY_MISMATCH") from None

    def _generation(self, name: object) -> Path:
        if not isinstance(name, str):
            raise WorkspaceError("INVALID_GENERATION")
        try:
            if str(uuid.UUID(name)) != name:
                raise ValueError
        except ValueError:
            raise WorkspaceError("INVALID_GENERATION") from None
        return self.root / name

    def _verify_generation(self, path: Path, *, region: str | None = None) -> None:
        boundary = self._identity_region() if region is None else region
        tree_digest(path)
        if not path.is_dir():
            raise WorkspaceError("INVALID_CURRENT_POINTER")
        for item in (path, *path.rglob("*")):
            if not io.private(item):
                raise WorkspaceError("UNSAFE_GENERATION")
        marker = load_metadata(path / "generation.json")
        profile = load_metadata(path / "data" / self.game / "profile.json")
        actual = self._profile_region(profile, boundary)
        expected = {
            "authority": "hoyo-roster/generation-v2",
            "scope": self.scope_digest,
            "identity": self._identity_document(actual),
            "generation": path.name,
            "data_digest": tree_digest(path / "data"),
            "contracts_digest": hashlib.sha256(
                (tree_digest(path / "schemas") + tree_digest(path / "tools")).encode()
            ).hexdigest(),
        }
        if marker != expected:
            raise WorkspaceError("GENERATION_OWNERSHIP_UNPROVEN")
        if actual is not None and boundary is None:
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
        self._require_lock()
        self.commit_state = "NOT_COMMITTED"
        self.cleanup_pending = False
        region = self.candidate_region
        root = game_dir.parent.parent
        if root.parent != self.candidate_base or game_dir != root / "data" / self.game:
            raise WorkspaceError("CANDIDATE_OWNERSHIP_UNPROVEN")
        self._generation(root.name)
        key = next(key for key, game in GAMES.items() if game == self.game)
        module = importlib.import_module(f"hoyo_roster.games.{key}.candidate")
        evidence = module.inspect_candidates(
            candidate_base=self.candidate_base, uid=self._uid, region=region
        )
        if not any(
            item.run_id == root.name and item.status == "VALIDATED" and item.cleanup_allowed
            for item in evidence
        ):
            raise WorkspaceError("CANDIDATE_OWNERSHIP_UNPROVEN")
        self._install(game_dir)

    def logout(self) -> None:
        self._require_lock()
        if self.browser_profile.exists():
            if not io.private(self.browser_profile, 0o700):
                raise WorkspaceError("UNSAFE_BROWSER_PROFILE")
            # Browser must be closed; never remove live or stale singleton authority.
            if any(
                (self.browser_profile / name).is_symlink() or (self.browser_profile / name).exists()
                for name in ("SingletonLock", "SingletonSocket", "SingletonCookie")
            ):
                raise WorkspaceError("BROWSER_PROFILE_IN_USE_OR_STALE")
            remove_owned_tree(self.browser_profile)
        for path in (self.auth_root / "identity.key", self.auth_root / "verified-identity.json"):
            if os.path.lexists(path):
                metadata = path.lstat()
                if not stat.S_ISREG(metadata.st_mode) or not io.private(path, 0o600):
                    raise WorkspaceError("UNSAFE_IDENTITY_STATE")
                path.unlink()

    def _verify_private_metadata(self, path: Path) -> None:
        if not path.is_file() or not io.private(path, 0o600):
            raise WorkspaceError("UNSAFE_METADATA")

    def _require_lock(self) -> None:
        if not self._locked:
            raise WorkspaceError("WORKSPACE_LOCK_REQUIRED")

    def _sync_pointer_directory(self) -> None:
        io.sync_directory(self.root)

    def _inventory(self, path: Path) -> dict[str, str]:
        tree_digest(path)
        return {
            item.relative_to(path).as_posix(): (
                "directory" if item.is_dir() else hashlib.sha256(item.read_bytes()).hexdigest()
            )
            for item in sorted(path.rglob("*"))
        }

    def _inventory_digest(self, inventory: object) -> str:
        return hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest()

    def _verify_inventory_document(self, inventory: object) -> None:
        if not isinstance(inventory, dict) or not inventory:
            raise WorkspaceError("UNOWNED_CLEANUP_RESIDUE")
        for name, digest in inventory.items():
            if not isinstance(name, str):
                raise WorkspaceError("UNOWNED_CLEANUP_RESIDUE")
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != name:
                raise WorkspaceError("UNOWNED_CLEANUP_RESIDUE")
            if not isinstance(digest, str) or (
                digest != "directory" and re.fullmatch("[0-9a-f]{64}", digest) is None
            ):
                raise WorkspaceError("UNOWNED_CLEANUP_RESIDUE")
            for parent in relative.parents:
                if parent != Path(".") and inventory.get(parent.as_posix()) != "directory":
                    raise WorkspaceError("UNOWNED_CLEANUP_RESIDUE")
        required_files = ["generation.json", "data/manifest.json", "tools/validate_repository.py"]
        for game in GAMES.values():
            required_files.extend((f"data/{game}/profile.json", f"data/{game}/roster.json"))
        if any(inventory.get(name) in (None, "directory") for name in required_files):
            raise WorkspaceError("UNOWNED_CLEANUP_RESIDUE")

    def _verify_cleanup_inventory(self, path: Path, inventory: object, identity: object) -> None:
        # A partially deleted generation uses the durable, previously validated inventory.
        self._verify_inventory_document(inventory)
        if not set(self._inventory(path).items()).issubset(set(inventory.items())):
            raise WorkspaceError("CLEANUP_RESIDUE_CHANGED")
        for item in (path, *path.rglob("*")):
            if not io.private(item):
                raise WorkspaceError("UNSAFE_CLEANUP_RESIDUE")
        marker = path / "generation.json"
        if os.path.lexists(marker):
            value = load_metadata(marker)
            if (
                not isinstance(value, dict)
                or value.get("authority") != "hoyo-roster/generation-v2"
                or value.get("scope") != self.scope_digest
                or value.get("generation") != path.name
                or value.get("identity") != identity
            ):
                raise WorkspaceError("GENERATION_OWNERSHIP_UNPROVEN")
        profile = path / "data" / self.game / "profile.json"
        if os.path.lexists(profile):
            self._profile_region(load_metadata(profile), self._identity_region())

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
        """Preflight the entire operation, then persist the pointer, then clean up."""
        self._require_lock()
        self.commit_state = "NOT_COMMITTED"
        self.cleanup_pending = False
        target, cleanup, has_journal = self._recovery_plan(generation)
        # No validation below this point: rejection cannot switch current or delete data.
        try:
            if generation is not None:
                atomic_write_metadata(self.current_file, {"generation": target.name})
            else:
                self._sync_pointer_directory()
        except AtomicWriteError:
            self.commit_state = "DURABILITY_UNKNOWN"
            raise WorkspaceError("COMMIT_DURABILITY_UNKNOWN") from None
        except OSError:
            if generation is None:
                self.commit_state = "DURABILITY_UNKNOWN"
                raise WorkspaceError("COMMIT_DURABILITY_UNKNOWN") from None
            raise WorkspaceError("RECOVERY_COMMIT_FAILED") from None
        self.commit_state = "COMMITTED"
        return self._finish_recovery(target, cleanup, has_journal)

    def _recovery_plan(self, generation: str | None) -> tuple[Path, list[Path], bool]:
        self._identity_region()
        try:
            current = self.current()
        except WorkspaceError:
            current = None
            if generation is None:
                raise WorkspaceError("RECOVERY_GENERATION_REQUIRED") from None
        target = self._generation(generation) if generation is not None else current
        if target is None:
            raise WorkspaceError("WORKSPACE_NOT_INITIALIZED")
        self._verify_generation(target)
        cleanup = []
        has_journal = os.path.lexists(self.transaction_file)
        if has_journal:
            self._verify_private_metadata(self.transaction_file)
            journal = load_metadata(self.transaction_file)
            if (
                not isinstance(journal, dict)
                or set(journal)
                != {"authority", "scope", "identity", "old", "new", "inventories", "evidence"}
                or journal.get("authority") != "hoyo-roster/transaction-v2"
                or journal.get("scope") != self.scope_digest
                or journal.get("identity") != self._identity_document(self._identity_region())
            ):
                raise WorkspaceError("UNOWNED_TRANSACTION")
            new = self._generation(journal["new"])
            old = self._generation(journal["old"]) if journal["old"] is not None else None
            targets = [path for path in (old, new) if path is not None]
            if (
                old == new
                or target not in targets
                or (current is not None and current not in targets)
            ):
                raise WorkspaceError("RECOVERY_REFERENCE_CONFLICT")
            # Even a corrupt generation can leave a readable reference. Do not erase
            # an unrelated pointer just because its referenced data failed validation.
            if os.path.lexists(self.current_file):
                try:
                    pointer = load_metadata(self.current_file)
                except MetadataError:
                    pointer = None
                if isinstance(pointer, dict) and "generation" in pointer:
                    reference = self._generation(pointer["generation"])
                    if reference not in targets:
                        raise WorkspaceError("RECOVERY_REFERENCE_CONFLICT")
            inventories = journal["inventories"]
            if not isinstance(inventories, dict) or set(inventories) != {p.name for p in targets}:
                raise WorkspaceError("UNOWNED_TRANSACTION")
            evidence = journal["evidence"]
            if not isinstance(evidence, dict) or set(evidence) != set(inventories):
                raise WorkspaceError("UNOWNED_TRANSACTION")
            for path in targets:
                inventory = inventories[path.name]
                self._verify_inventory_document(inventory)
                proof = evidence[path.name]
                if not isinstance(proof, dict) or set(proof) != {"identity", "inventory_digest"}:
                    raise WorkspaceError("UNOWNED_TRANSACTION")
                identity = proof["identity"]
                if identity not in (
                    self._identity_document(None),
                    self._identity_document(self._identity_region()),
                ) or proof["inventory_digest"] != self._inventory_digest(inventory):
                    raise WorkspaceError("RECOVERY_INVENTORY_MISMATCH")
                if path == target:
                    if (
                        inventory != self._inventory(path)
                        or load_metadata(path / "generation.json")["identity"] != identity
                    ):
                        raise WorkspaceError("RECOVERY_INVENTORY_MISMATCH")
                elif os.path.lexists(path):
                    self._verify_cleanup_inventory(path, inventory, identity)
                    cleanup.append(path)
        return target, cleanup, has_journal

    def _finish_recovery(self, target: Path, cleanup: list[Path], has_journal: bool) -> Path:
        self.cleanup_pending = False
        try:
            for path in cleanup:
                remove_owned_tree(path)
            if has_journal:
                self.transaction_file.unlink()
                self._sync_pointer_directory()
        except (OSError, MetadataError):
            self.cleanup_pending = True
        return target

    def _install(self, game_dir: Path | None) -> Path:
        self._require_lock()
        if os.path.lexists(self.transaction_file):
            raise WorkspaceError("RECOVERY_REQUIRED")
        self.commit_state = "NOT_COMMITTED"
        if not os.path.lexists(self.identity_file):
            if any(self.root.iterdir()):
                raise WorkspaceError("IDENTITY_BOUNDARY_UNPROVEN")
            atomic_write_metadata(self.identity_file, self._identity_document(None))
        region = self._identity_region()
        old = self.current()
        allowed = {
            "current.json",
            "candidates",
            "workspace-identity.json",
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
                actual = self._profile_region(account, region)
                if actual is not None:
                    region = actual
                target = new / "data" / self.game
                remove_owned_tree(target)
                shutil.copytree(existing, target)
            validate_workspace(new)
            write_json(
                new / "generation.json",
                {
                    "authority": "hoyo-roster/generation-v2",
                    "scope": self.scope_digest,
                    "identity": self._identity_document(
                        self._profile_region(
                            load_metadata(new / "data" / self.game / "profile.json"), region
                        )
                    ),
                    "generation": new.name,
                    "data_digest": tree_digest(new / "data"),
                    "contracts_digest": hashlib.sha256(
                        (tree_digest(new / "schemas") + tree_digest(new / "tools")).encode()
                    ).hexdigest(),
                },
            )
            for p in new.rglob("*"):
                io.set_private(p)
                if p.is_file():
                    descriptor = io.open_private(p, os.O_RDWR)
                    try:
                        io.sync_file(descriptor)
                    finally:
                        os.close(descriptor)
            for directory in (*reversed([p for p in new.rglob("*") if p.is_dir()]), new):
                io.sync_directory(directory)
            self._verify_generation(new, region=region)
            if region != self._identity_region():
                atomic_write_metadata(self.identity_file, self._identity_document(region))
            inventories = {
                path.name: self._inventory(path) for path in (old, new) if path is not None
            }
            journal_attempted = True
            atomic_write_metadata(
                self.transaction_file,
                {
                    "authority": "hoyo-roster/transaction-v2",
                    "scope": self.scope_digest,
                    "identity": self._identity_document(region),
                    "old": old.name if old else None,
                    "new": new.name,
                    "inventories": inventories,
                    "evidence": {
                        path.name: {
                            "identity": load_metadata(path / "generation.json")["identity"],
                            "inventory_digest": self._inventory_digest(inventories[path.name]),
                        }
                        for path in (old, new)
                        if path is not None
                    },
                },
            )
            _, cleanup, _ = self._recovery_plan(new.name)
            try:
                atomic_write_metadata(self.current_file, {"generation": new.name})
            except AtomicWriteError:
                self.commit_state = "DURABILITY_UNKNOWN"
                raise WorkspaceError("COMMIT_DURABILITY_UNKNOWN") from None
            self.commit_state = "COMMITTED"
            return self._finish_recovery(new, cleanup, True)
        except Exception:
            if self.commit_state == "PRE_COMMIT":
                self.commit_state = "NOT_COMMITTED"
            raise
        finally:
            # Once a journal may exist, retain both generations for verified recovery.
            if not journal_attempted and new.exists():
                remove_owned_tree(new)
