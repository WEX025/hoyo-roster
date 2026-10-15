"""Local user configuration and replaceable snapshot storage; no publication authority."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from collections.abc import Iterator, Callable
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol, Literal

from .errors import WorkspaceError
from . import paths
from . import platform_io as io
import portalocker
from .storage.candidate import CandidateScope, CandidatePolicy, inspect_candidates, REGIONS
from .validation import ValidationFailure, run_validator, copy_game_workspace
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


CommitState = Literal["NOT_COMMITTED", "PRE_COMMIT", "DURABILITY_UNKNOWN", "COMMITTED"]


@dataclass(frozen=True, slots=True)
class CommitResult:
    state: CommitState
    cleanup_pending: bool


class SnapshotStore(Protocol):
    candidate_base: Path

    @property
    def commit_state(self) -> CommitState: ...

    @property
    def cleanup_pending(self) -> bool: ...

    def commit(self, game_dir: Path) -> CommitResult: ...

    def cleanup_candidate(self, cleanup: Callable[[], None]) -> None: ...


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
    storage_root: Path | None = None,
) -> UserConfiguration:
    """Resolve an explicit selector without reading or writing any config file."""
    if game not in GAMES:
        raise WorkspaceError("INVALID_GAME")
    if not isinstance(uid, str) or not _UID.fullmatch(uid):
        raise WorkspaceError("INVALID_UID")
    storage_root = paths.default_storage_root() if storage_root is None else storage_root
    if not storage_root.expanduser().is_absolute():
        raise WorkspaceError("INVALID_STORAGE_ROOT")
    if io.WINDOWS and str(storage_root).startswith("\\\\"):
        raise WorkspaceError("UNSUPPORTED_PRIVATE_FILESYSTEM")
    digest = hashlib.sha256(f"direct:v1:{game}:{uid}".encode()).hexdigest()
    alias = f"direct-{game}-{digest[:48]}"
    return UserConfiguration(alias, storage_root, {game: uid})


def validate_workspace(root: Path, game: str | None = None) -> None:
    try:
        run_validator(
            root, scope_game=game, validator_root=Path(__file__).resolve().parent / "contracts"
        )
    except ValidationFailure as error:
        raise WorkspaceError(
            "WORKSPACE_VALIDATION_FAILED"
            if error.code == "VALIDATOR_REJECTED"
            else "WORKSPACE_" + error.code
        ) from None


def empty_workspace(source: Path, destination: Path, game: str | None = None) -> None:
    """Copy only public contracts; never read source account data."""
    private_directory(destination)
    if game is not None:
        copy_game_workspace(source, destination, game)
        return
    schemas = source / "schemas"
    tree_digest(schemas)
    shutil.copytree(schemas, destination / "schemas")
    (destination / "tools").mkdir()
    copy_regular_file(
        source / "tools/validate_repository.py", destination / "tools/validate_repository.py"
    )
    # The packaged, validated public empty template is the sole initialization source.
    tree_digest(source / "data")
    shutil.copytree(source / "data", destination / "data")


def write_json(path: Path, document: object) -> None:
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    io.set_private(path, 0o600)


class LocalSnapshotStore:
    """One current validated generation per user, game, and UID; atomic pointer install."""

    def __init__(
        self,
        source: Path,
        config: UserConfiguration,
        game: str,
        *,
        candidate_policy: CandidatePolicy | None = None,
    ) -> None:
        self._candidate_policy = candidate_policy
        self.source = source.resolve()
        if not _ALIAS.fullmatch(config.user) or game not in GAMES:
            raise WorkspaceError("INVALID_CONFIG")
        self.game = GAMES[game]
        self._uid = config.uid(game)
        if not _UID.fullmatch(self._uid):
            raise WorkspaceError("INVALID_CONFIG")
        try:
            io.check_components(config.storage_root)
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
        # A cloned data/ container is usually 0755 because of its tracked placeholder.
        # Leave it unchanged; all identity directories below it must still be 0700.
        if resolved == repository_data and config.storage_root.exists():
            if not io.repository_container_safe(config.storage_root):
                raise WorkspaceError("UNSAFE_PRIVATE_DIRECTORY")
            root = config.storage_root
        else:
            root = private_directory(config.storage_root)
        self.user_root = private_directory(root / "game-results-v3" / config.user)
        key = hashlib.sha256(f"{game}:{self._uid}".encode()).hexdigest()
        self.root = private_directory(self.user_root / game / key)
        self.candidate_base = self.root / "candidates"
        if owner is not None and any(
            os.path.lexists(self.root / name)
            for name in ("browser-profile", "identity.key", "verified-identity.json")
        ):
            raise WorkspaceError("AUTH_STATE_IN_RESULT_DIRECTORY")
        self.current_file = self.root / "current.json"
        self._locked = False
        self._commit_state = "NOT_COMMITTED"
        self._cleanup_pending = False
        self.transaction_file = self.root / "transaction.json"
        self.identity_file = self.root / "workspace-identity.json"

    @contextmanager
    def locked(self) -> Iterator[LocalSnapshotStore]:
        # Only this game's result scope; the auth service owns the account/Profile lock.
        with self._lock_directory(self.user_root):
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
            "authority": "hoyo-roster/generation-v3",
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
        validate_workspace(path, self.game)

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

    @property
    def commit_state(self) -> CommitState:
        return self._commit_state

    @property
    def cleanup_pending(self) -> bool:
        return self._cleanup_pending

    def prepare(self) -> Path:
        self._preflight()
        current = self.current()
        return current if current is not None else self._install(None)

    def commit(self, game_dir: Path) -> CommitResult:
        self._require_lock()
        self._commit_state = "NOT_COMMITTED"
        self._cleanup_pending = False
        region = self.candidate_region
        root = game_dir.parent.parent
        if root.parent != self.candidate_base or game_dir != root / "data" / self.game:
            raise WorkspaceError("CANDIDATE_OWNERSHIP_UNPROVEN")
        self._generation(root.name)
        if (
            self._candidate_policy is None
            or self._candidate_policy.target_relative != Path("data") / self.game
        ):
            raise WorkspaceError("CANDIDATE_POLICY_REQUIRED")
        policy = replace(
            self._candidate_policy, scope=CandidateScope.identity(self.game, self._uid, region)
        )
        evidence = inspect_candidates(policy, candidate_base=self.candidate_base)
        if not any(
            item.run_id == root.name and item.status == "VALIDATED" and item.cleanup_allowed
            for item in evidence
        ):
            raise WorkspaceError("CANDIDATE_OWNERSHIP_UNPROVEN")
        self._install(game_dir)
        return CommitResult(self.commit_state, self.cleanup_pending)

    def cleanup_candidate(self, cleanup: Callable[[], None]) -> None:
        """Drain verified candidate cleanup only after a confirmed commit."""
        self._require_lock()
        if self.commit_state != "COMMITTED":
            return
        try:
            cleanup()
        except Exception:
            self._cleanup_pending = True

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
        required_files.extend((f"data/{self.game}/profile.json", f"data/{self.game}/roster.json"))
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
                or value.get("authority") != "hoyo-roster/generation-v3"
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
        self._commit_state = "NOT_COMMITTED"
        self._cleanup_pending = False
        target, cleanup, has_journal = self._recovery_plan(generation)
        # No validation below this point: rejection cannot switch current or delete data.
        try:
            if generation is not None:
                atomic_write_metadata(self.current_file, {"generation": target.name})
            else:
                self._sync_pointer_directory()
        except AtomicWriteError:
            self._commit_state = "DURABILITY_UNKNOWN"
            raise WorkspaceError("COMMIT_DURABILITY_UNKNOWN") from None
        except OSError:
            if generation is None:
                self._commit_state = "DURABILITY_UNKNOWN"
                raise WorkspaceError("COMMIT_DURABILITY_UNKNOWN") from None
            raise WorkspaceError("RECOVERY_COMMIT_FAILED") from None
        self._commit_state = "COMMITTED"
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
                or journal.get("authority") != "hoyo-roster/transaction-v3"
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
        self._cleanup_pending = False
        try:
            for path in cleanup:
                remove_owned_tree(path)
            if has_journal:
                self.transaction_file.unlink()
                self._sync_pointer_directory()
        except (OSError, MetadataError):
            self._cleanup_pending = True
        return target

    def _preflight(self) -> None:
        self._require_lock()
        if os.path.lexists(self.transaction_file):
            raise WorkspaceError("RECOVERY_REQUIRED")
        if not os.path.lexists(self.identity_file):
            if any(self.root.iterdir()):
                raise WorkspaceError("IDENTITY_BOUNDARY_UNPROVEN")
            atomic_write_metadata(self.identity_file, self._identity_document(None))
        self._identity_region()
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

    def _install(self, game_dir: Path | None) -> Path:
        self._commit_state = "NOT_COMMITTED"
        self._preflight()
        region = self._identity_region()
        old = self.current()
        new = self.root / str(uuid.uuid4())
        journal_attempted = False
        self._commit_state = "PRE_COMMIT"
        try:
            empty_workspace(self.source, new, self.game)
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
            validate_workspace(new, self.game)
            write_json(
                new / "generation.json",
                {
                    "authority": "hoyo-roster/generation-v3",
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
                    "authority": "hoyo-roster/transaction-v3",
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
                self._commit_state = "DURABILITY_UNKNOWN"
                raise WorkspaceError("COMMIT_DURABILITY_UNKNOWN") from None
            self._commit_state = "COMMITTED"
            return self._finish_recovery(new, cleanup, True)
        except Exception:
            if self._commit_state == "PRE_COMMIT":
                self._commit_state = "NOT_COMMITTED"
            raise
        finally:
            # Once a journal may exist, retain both generations for verified recovery.
            if not journal_attempted and new.exists():
                remove_owned_tree(new)
