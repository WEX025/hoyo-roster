"""Shared observation validation and atomic storage, with injected game policies."""

from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
import re

import portalocker

from . import paths, platform_io as io
from .errors import AuthenticationRequired, SourceDataInvalid, WrongAccount, WorkspaceError
from .game_runtime import GameResult, RoleResult
from .models import OwnedRole
from .storage.common import AtomicWriteError, MetadataError, atomic_write_metadata, load_metadata
from .user_workspace import direct_configuration, private_directory


def text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 65536:
        raise SourceDataInvalid()
    return value


def number(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,4}", value):
        raise SourceDataInvalid()
    return int(value)


def identity(value, game):
    if (
        not isinstance(value, dict)
        or set(value) != {"uid", "nickname", "region"}
        or not isinstance(value["uid"], str)
        or not re.fullmatch(r"[1-9][0-9]{0,31}", value["uid"])
    ):
        raise SourceDataInvalid()
    text(value["nickname"])
    text(value["region"])
    return OwnedRole(game, value["uid"], value["region"])


def roster(value):
    if not isinstance(value, dict) or set(value) not in ({"rows"}, {"rows", "excluded_unowned"}):
        raise SourceDataInvalid()
    rows = value["rows"]
    if not isinstance(rows, list) or not rows or len(rows) > 1000:
        raise SourceDataInvalid()
    result = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"name", "level"}:
            raise SourceDataInvalid()
        name, level = text(row["name"]), row["level"]
        if not isinstance(level, str) or not re.fullmatch(r"Lv\.[1-9][0-9]{0,3}", level):
            raise SourceDataInvalid()
        result.append({"name": name, "level": int(level[3:])})
    if len({r["name"] for r in result}) != len(result):
        raise SourceDataInvalid()
    if "excluded_unowned" in value and (
        type(value["excluded_unowned"]) is not int or value["excluded_unowned"] < 0
    ):
        raise SourceDataInvalid()
    return sorted(result, key=lambda row: row["name"])


def validate_snapshot(value, game, normalize):
    required = {"format", "game", "state", "scope", "profile", "roster", "details", "synced_at"}
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value["format"] != "hoyo-roster-page-v1"
        or value["game"] != game
        or value["state"] != "partial"
        or value["scope"] != "rendered-owned-characters"
    ):
        raise SourceDataInvalid()
    identity(value["profile"], game)
    # Validate the stored shape without coercing numeric values or changing ordering.
    rows = value["roster"]
    if not isinstance(rows, list) or not rows:
        raise SourceDataInvalid()
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"name", "level"}
            or type(row["level"]) is not int
            or not 1 <= row["level"] <= 9999
        ):
            raise SourceDataInvalid()
        text(row["name"])
    names = [r["name"] for r in rows]
    if names != sorted(set(names)) or not isinstance(value["details"], list):
        raise SourceDataInvalid()
    details = value["details"]
    if [d.get("name") for d in details if isinstance(d, dict)] != names:
        raise SourceDataInvalid()
    for detail in details:
        normalize(detail, stored=True)
        if "level" in detail and detail["level"] != next(
            r["level"] for r in rows if r["name"] == detail["name"]
        ):
            raise SourceDataInvalid()
    try:
        stamp = datetime.fromisoformat(value["synced_at"].replace("Z", "+00:00"))
        if stamp.utcoffset().total_seconds() != 0:
            raise ValueError()
    except (TypeError, ValueError, AttributeError):
        raise SourceDataInvalid() from None


class PageSnapshotStore:
    """One atomically replaced private snapshot; never touch canonical generations."""

    def __init__(self, config, role, normalize):
        selected = config.storage_root.expanduser()
        io.check_components(selected)
        resolved = selected.resolve()
        repo = paths.project_repository()
        owner = paths.containing_repository(resolved)
        module = Path(__file__).resolve().parent
        if (
            resolved.is_relative_to(module)
            or module.is_relative_to(resolved)
            or (repo is not None and repo.is_relative_to(resolved))
            or (owner is not None and (repo is None or not resolved.is_relative_to(repo / "data")))
        ):
            raise WorkspaceError("UNSAFE_STORAGE_ROOT")
        if repo is not None and resolved == repo / "data" and selected.exists():
            if not io.repository_container_safe(selected):
                raise WorkspaceError("UNSAFE_PRIVATE_DIRECTORY")
            base = selected
        else:
            base = private_directory(selected)
        self.user_root = private_directory(base / "page-observations-v1" / config.user)
        digest = sha256(f"{role.game}:{role.uid}".encode()).hexdigest()
        self.root = private_directory(self.user_root / role.game / digest)
        self.current_file = self.root / "snapshot.json"
        self.role, self.normalize = role, normalize

    @contextmanager
    def locked(self):
        try:
            with io.exclusive_lock(self.user_root / "workspace.lock"):
                yield self
        except portalocker.exceptions.LockException:
            raise WorkspaceError("USER_WORKSPACE_BUSY") from None
        except (io.UnsafePath, OSError):
            raise WorkspaceError("UNSAFE_LOCK") from None

    def save(self, snapshot):
        validate_snapshot(snapshot, self.role.game, self.normalize)
        if identity(snapshot["profile"], self.role.game) != self.role:
            raise WrongAccount()
        if self.current_file.exists():
            previous = load_metadata(self.current_file, private=True)
            validate_snapshot(previous, self.role.game, self.normalize)
            if identity(previous["profile"], self.role.game) != self.role:
                raise WrongAccount()
            old = {r["name"]: r["level"] for r in previous["roster"]}
            new = {r["name"]: r["level"] for r in snapshot["roster"]}
            if not old.keys() <= new.keys() or any(new[n] < level for n, level in old.items()):
                raise SourceDataInvalid()
            previously_read = {
                d["name"] for d in previous["details"] if d.get("status") != "unsupported"
            }
            now_read = {d["name"] for d in snapshot["details"] if d.get("status") != "unsupported"}
            if not previously_read <= now_read:
                raise SourceDataInvalid()
            if {k: v for k, v in previous.items() if k != "synced_at"} == {
                k: v for k, v in snapshot.items() if k != "synced_at"
            }:
                return False
        atomic_write_metadata(self.current_file, snapshot)
        return True


async def run_page_game(args, session, *, game, source, normalize, batch_size):
    async def check():
        value = await source.read("identity")
        return identity(value, game), value

    async def pinned_check():
        return (await check())[0]

    try:
        role, profile = await check()
    except AuthenticationRequired:
        recovered = await session.recover(verify_current=check)
        role, profile = recovered
    session.pin(game, (role,), lambda: _role_tuple(pinned_check))
    if args.uid is not None and args.uid != role.uid:
        raise WrongAccount()
    if args.operation == "discover":
        return GameResult(game, "PASS", (RoleResult(role, "VERIFIED"),))

    async def collect():
        observed_role, current_profile = await check()
        if observed_role != role:
            raise WrongAccount()
        params = {"uid": role.uid, "region": role.region}
        rows = roster(await source.read("roster", params))
        details = []
        for start in range(0, len(rows), batch_size):
            names = [r["name"] for r in rows[start : start + batch_size]]
            chunk = await source.read("detail", {**params, "names": names})
            if (
                not isinstance(chunk, dict)
                or set(chunk) != {"details"}
                or not isinstance(chunk["details"], list)
            ):
                raise SourceDataInvalid()
            mapped = [normalize(raw) for raw in chunk["details"]]
            if len(mapped) != len(names) or {d["name"] for d in mapped} != set(names):
                raise SourceDataInvalid()
            details.extend(mapped)
            print(f"PAGE_DETAILS_DONE={len(details)}", flush=True)
        if (await check())[0] != role:
            raise WrongAccount()
        details.sort(key=lambda d: d["name"])
        return {
            "format": "hoyo-roster-page-v1",
            "game": game,
            "state": "partial",
            "scope": "rendered-owned-characters",
            "profile": current_profile,
            "roster": rows,
            "details": details,
            "synced_at": "2000-01-01T00:00:00Z",
        }

    config = direct_configuration(game, role.uid, storage_root=args.storage_root)
    try:
        store = PageSnapshotStore(config, role, normalize)
    except (MetadataError, io.UnsafePath, OSError):
        raise WorkspaceError("UNSAFE_STORAGE_ROOT") from None
    with store.locked():
        for attempt in range(2):
            try:
                print(f"PAGE_COLLECTION={game}\nPAGE_PASS=1", flush=True)
                first = await collect()
                print("PAGE_PASS=2", flush=True)
                second = await collect()
                if first != second:
                    raise SourceDataInvalid()
                validate_snapshot(second, game, normalize)
                second["synced_at"] = (
                    datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                )
                changed = store.save(second)
                break
            except AuthenticationRequired:
                if attempt:
                    raise
                print("AUTH_RECOVERY=RESTART_CURRENT_GAME", flush=True)
                await session.recover()
            except AtomicWriteError:
                return GameResult(
                    game,
                    "FAIL",
                    (
                        RoleResult(
                            role,
                            "FAIL",
                            "ATOMIC_DURABILITY_UNKNOWN",
                            result_scope=store.root,
                            current_result=store.current_file,
                            commit_state="DURABILITY_UNKNOWN",
                        ),
                    ),
                    error_code="ATOMIC_DURABILITY_UNKNOWN",
                )
            except (MetadataError, io.UnsafePath, OSError):
                return GameResult(
                    game,
                    "FAIL",
                    (RoleResult(role, "FAIL", "PAGE_SAVE_FAILED", result_scope=store.root),),
                    error_code="PAGE_SAVE_FAILED",
                )
    print(
        f"PAGE_DETAILS_UNAVAILABLE={sum(d.get('status') == 'unsupported' for d in second['details'])}",
        flush=True,
    )
    print(
        f"SNAPSHOT_KIND=OFFICIAL_PAGE\nCOLLECTION_STATUS=PARTIAL\nPAGE_CHARACTER_COUNT={len(second['roster'])}",
        flush=True,
    )
    return GameResult(
        game,
        "PASS",
        (
            RoleResult(
                role,
                "SAVED" if changed else "UNCHANGED",
                result_scope=store.root,
                current_result=store.current_file,
                commit_state="COMMITTED",
            ),
        ),
    )


async def _role_tuple(check):
    return (await check(),)
