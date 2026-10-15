"""First login discovers owned roles; exact identity collection remains separate."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import math
import os
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import paths, platform_io as io
from .config import Settings
from .errors import ConfigurationError, LoginBindingFailed
from .login import ExpectedIdentity, coordinate_login
from .models import LoginProbeResult, LoginProbeStatus
from .progress import OPTIONS, options_from_args
from .request_gate import RequestGate
from .sync import operation_browser
from .storage.common import atomic_write_metadata, load_metadata
from .user_workspace import (
    LocalSnapshotStore,
    WorkspaceError,
    direct_configuration,
    private_directory,
)

SUPPORTED_GAMES = ("genshin", "zzz")


def mark_scope(scope: Path, game: str, state: str) -> None:
    atomic_write_metadata(
        scope / "discovery-state.json",
        {
            "authority": "hoyo-roster/discovery-v1",
            "game": game,
            "state": state,
            "scope": hashlib.sha256(str(scope).encode()).hexdigest(),
        },
    )


def resume_scope(parent: Path, game: str, run_id: str) -> Path:
    """Only an explicit owned, confirmed-closed new scope; never search old profiles."""
    try:
        if str(uuid.UUID(run_id)) != run_id:
            raise ValueError
        scope = parent / run_id
        io.check_components(scope)
        if not scope.is_dir() or not io.private(scope):
            raise ValueError
        marker = load_metadata(scope / "discovery-state.json", private=True)
        expected = {
            "authority": "hoyo-roster/discovery-v1",
            "game": game,
            "state": "CLOSED",
            "scope": hashlib.sha256(str(scope).encode()).hexdigest(),
        }
        if marker != expected:
            raise ValueError
        from .browser_runtime import profile_in_use, saved_channel

        profile = scope / "browser-profile"
        io.check_components(profile)
        if not profile.is_dir() or not io.private(profile):
            raise ValueError
        channel = saved_channel(profile)
        if channel is None:
            raise ValueError
        io.check_components(profile / channel)
        if not (profile / channel).is_dir() or not io.private(profile / channel):
            raise ValueError
        if profile_in_use(profile):
            raise WorkspaceError("USER_WORKSPACE_BUSY")
        return scope
    except WorkspaceError:
        raise
    except Exception:
        raise WorkspaceError("DISCOVERY_RESUME_UNPROVEN") from None


@dataclass(frozen=True, repr=False)
class DiscoveredRole:
    game: str
    uid: str = field(repr=False)
    region: str


def binding_roles(game: str, data: object) -> tuple[DiscoveredRole, ...]:
    """Validate every matching official role, including duplicates and regions."""
    if game not in SUPPORTED_GAMES or not isinstance(data, dict):
        raise LoginBindingFailed("Discovery Binding was invalid")
    items = data.get("list")
    if not isinstance(items, list):
        raise LoginBindingFailed("Discovery Binding list was invalid")
    result = []
    seen = set()
    module = importlib.import_module(f"hoyo_roster.games.{game}.login_provider")
    for item in items:
        if not isinstance(item, dict):
            raise LoginBindingFailed("Discovery role was invalid")
        if item.get("game_biz") != module.GAME_BIZ:
            continue
        try:
            if game == "genshin":
                uid = module.positive_id(item.get("game_uid"))
                role = module.select_binding(data, uid)
                region = role["server"]
                supported = module.SUPPORTED_REGIONS
            else:
                uid = module.parse_role_uid(item.get("game_uid"))
                role = module.select_role(data, uid)
                region = role.server
                supported = {module.EXPECTED_REGION}
            if region not in supported or uid in seen:
                raise ValueError
        except Exception:
            raise LoginBindingFailed("Discovery role identity was invalid or unsupported") from None
        seen.add(uid)
        result.append(DiscoveredRole(game, uid, region))
    return tuple(result)


class DiscoveryProvider:
    """An explicit all-owned-role selector, never authority for a business save.

    Like the existing first-use Genshin selector, the coordinator digest selects
    a workflow. PASS here requires a successful official Binding and fully
    validated roles. Each returned role must subsequently pass the unchanged
    exact game/UID/region provider before any business collection.
    """

    def __init__(self, game, routine):
        self.game = game
        self.routine = routine
        self.roles = None
        self.selector = hashlib.sha256(f"owned-role-discovery:v1:{game}".encode()).hexdigest()

    async def expected_identity(self, repo_root):
        return ExpectedIdentity(self.game, self.selector)

    async def initialize_session(self):
        await self.routine.initialize_session()

    async def probe(self, context):
        if context is not self.routine.context:
            raise ConfigurationError("Discovery browser context changed")
        module = importlib.import_module(f"hoyo_roster.games.{self.game}.login_provider")
        try:
            data = (
                await self.routine.collector.binding()
                if self.game == "genshin"
                else dict(await self.routine.endpoints.get_binding())
            )
        except module.AuthenticationError:
            return LoginProbeResult(LoginProbeStatus.AUTH_REQUIRED)
        except module.RateLimitedError:
            return LoginProbeResult(LoginProbeStatus.RATE_LIMITED)
        except module.RiskControlError:
            return LoginProbeResult(LoginProbeStatus.RISK_CONTROL)
        except module.RequestTransportError:
            return LoginProbeResult(LoginProbeStatus.NETWORK_ERROR)
        except module.ApiResponseError:
            return LoginProbeResult(LoginProbeStatus.BINDING_FAILED)
        self.roles = binding_roles(self.game, data)
        return LoginProbeResult(LoginProbeStatus.PASS, self.selector)

    def login_page(self, fallback):
        return self.routine.login_page(fallback)

    async def prepare_login_or_session(self, context, page, request_gate):
        await self.routine.prepare_login_or_session(context, page, request_gate)

    async def finish_login_or_session(self, context, page):
        await self.routine.finish_login_or_session(context, page)

    def on_login_success(self):
        pass


def discovery_root(value: Path | None, source: Path) -> Path:
    root = value if value is not None else paths.default_private_root()
    if not root.expanduser().is_absolute():
        raise WorkspaceError("INVALID_AUTH_ROOT")
    if io.WINDOWS and str(root).startswith("\\\\"):
        raise WorkspaceError("UNSUPPORTED_PRIVATE_FILESYSTEM")
    io.check_components(root)
    root = root.expanduser().absolute()
    resolved = root.resolve()
    if (
        paths.containing_repository(resolved) is not None
        or resolved.is_relative_to(source)
        or source.is_relative_to(resolved)
    ):
        raise WorkspaceError("UNSAFE_AUTH_ROOT")
    return private_directory(private_directory(root) / "onboarding")


def cleanup_new_scope(root: Path, parent: Path) -> None:
    """Only delete this invocation's UUID scope, inspecting metadata, never credentials."""
    if root.parent != parent or str(uuid.UUID(root.name)) != root.name:
        raise WorkspaceError("DISCOVERY_CLEANUP_UNPROVEN")
    io.check_components(root)
    if not io.private(root):
        raise WorkspaceError("DISCOVERY_CLEANUP_UNPROVEN")
    for directory, subdirs, files in os.walk(root, followlinks=False):
        for name in (*subdirs, *files):
            if io.is_redirect((Path(directory) / name).lstat()):
                raise WorkspaceError("DISCOVERY_CLEANUP_UNPROVEN")
    shutil.rmtree(root)


def adopt_new_profile(profile: Path, store: LocalSnapshotStore) -> bool:
    """Place only a newly created, closed onboarding profile; never overwrite one."""
    if not io.private(profile):
        raise WorkspaceError("UNSAFE_PRIVATE_DIRECTORY")
    from .browser_runtime import profile_in_use

    if profile_in_use(profile):
        raise WorkspaceError("USER_WORKSPACE_BUSY")
    io.check_components(store.browser_profile)
    if os.path.lexists(store.browser_profile):
        return False
    # Preserve the last discovery reservation when the session becomes scoped.
    source_state = profile.parent / "request-state.json"
    if source_state.exists():
        states = [load_metadata(source_state, private=True)]
        destination_state = store.auth_root / "request-state.json"
        if destination_state.exists():
            states.append(load_metadata(destination_state, private=True))
        for state in states:
            if (
                not isinstance(state, dict)
                or set(state) != {"next_allowed_at"}
                or type(state["next_allowed_at"]) not in {int, float}
                or not math.isfinite(state["next_allowed_at"])
            ):
                raise ConfigurationError("Discovery request state was invalid")
        atomic_write_metadata(
            destination_state,
            {"next_allowed_at": max(state["next_allowed_at"] for state in states)},
        )
    # Scope locks are held by the caller and the destination does not exist.
    profile.rename(store.browser_profile)
    io.sync_directory(store.browser_profile.parent)
    return True


async def discover_game(game, args, scope, source):
    browser = importlib.import_module(f"hoyo_roster.games.{game}.browser")
    providers = importlib.import_module(f"hoyo_roster.games.{game}.login_provider")
    profile = scope / "browser-profile"
    gate = RequestGate(Settings(external_root=scope))
    async with operation_browser(
        browser.persistent_browser(
            repo_root=source, browser_profile=profile, headed=not args.headless
        ),
        on_closed=lambda: mark_scope(scope, game, "CLOSED"),
    ) as session:
        routine_class = (
            providers.GenshinLoginProvider if game == "genshin" else providers.ZzzLoginProvider
        )
        routine = routine_class(session.context, session.page, gate, 2.0, headed=not args.headless)
        provider = DiscoveryProvider(game, routine)
        result = await coordinate_login(
            repo_root=source,
            context=session.context,
            page=session.page,
            request_gate=gate,
            providers=(provider,),
            headed=not args.headless,
            poll_interval=2.0,
            timeout=args.login_timeout,
        )
        if not result.success or provider.roles is None:
            raise LoginBindingFailed("Discovery Binding was not confirmed")
        roles = provider.roles
    return profile, roles


async def run_discovery(args) -> int:
    from .cli import CONTRACT_ROOT

    if args.game == "hsr":
        raise WorkspaceError("GAME_PAUSED")
    if args.uid is not None or args.operation not in {"discover", "login", "sync"}:
        raise WorkspaceError("ARGUMENTS_INVALID")
    if args.run_id is not None or args.generation is not None:
        raise WorkspaceError("ARGUMENTS_INVALID")
    if not math.isfinite(args.login_timeout) or not 0 < args.login_timeout <= 3600:
        raise WorkspaceError("INVALID_LOGIN_TIMEOUT")
    games = SUPPORTED_GAMES if args.game in {None, "all"} else (args.game,)
    resume_id = getattr(args, "resume_discovery", None)
    if resume_id is not None and args.game not in SUPPORTED_GAMES:
        raise WorkspaceError("INVALID_DISCOVERY_RESUME")
    parent = discovery_root(args.auth_root, CONTRACT_ROOT)
    failed = False
    role_count = 0
    token = OPTIONS.set(options_from_args(args))
    try:
        with io.exclusive_lock(parent / "workspace.lock"):
            for game in games:
                # Ask only if this game's session needs login. Interactive choices
                # are local to that login; explicit CLI SMS permission stays shared.
                OPTIONS.set(options_from_args(args))
                registry = parent / f"selected-{game}.json"
                if resume_id is None and args.operation != "discover" and os.path.lexists(registry):
                    roles = cached_roles(game, load_metadata(registry, private=True))
                    print(
                        f"GAME={game}\nDISCOVERY_SOURCE=LOCAL_SELECTION\nDISCOVERED_ROLES={len(roles)}",
                        flush=True,
                    )
                    if roles:
                        role_count += len(roles)
                        if await run_selected_roles(roles, args):
                            failed = True
                            break
                        continue
                    print("DISCOVERY_SELECTION=EMPTY_RECHECK", flush=True)
                print(f"GAME={game}\nOPERATION=discover", flush=True)
                scope = (
                    resume_scope(parent, game, resume_id)
                    if resume_id is not None
                    else private_directory(parent / str(uuid.uuid4()))
                )
                mark_scope(scope, game, "OPEN")
                cleanup_allowed = False
                try:
                    profile, roles = await discover_game(game, args, scope, CONTRACT_ROOT)
                    # discover_game returns only after actual close; also supports
                    # deterministic test transports without reading profile contents.
                    mark_scope(scope, game, "CLOSED")
                    print(f"DISCOVERED_ROLES={len(roles)}", flush=True)
                    role_count += len(roles)
                    if not roles:
                        print("GAME_STATUS=NO_ROLES\nCOLLECTION_STATUS=NOT_COLLECTED", flush=True)
                        cleanup_allowed = True
                        continue
                    for index, role in enumerate(roles):
                        config = direct_configuration(
                            game, role.uid, storage_root=args.storage_root, auth_root=args.auth_root
                        )
                        candidate = importlib.import_module(f"hoyo_roster.games.{game}.candidate")
                        store = LocalSnapshotStore(
                            CONTRACT_ROOT,
                            config,
                            game,
                            candidate_policy=candidate.candidate_policy(),
                        )
                        if index == 0:
                            with store.locked():
                                if not adopt_new_profile(profile, store):
                                    raise WorkspaceError("DISCOVERY_PROFILE_CONFLICT")
                    atomic_write_metadata(
                        registry,
                        {
                            "authority": "hoyo-roster/owned-role-selection-v1",
                            "game": game,
                            "roles": [{"uid": role.uid, "region": role.region} for role in roles],
                        },
                    )
                    cleanup_allowed = True
                    if args.operation != "discover" and await run_selected_roles(roles, args):
                        failed = True
                finally:
                    # A failed/uncertain browser close must preserve its new scope.
                    if cleanup_allowed:
                        cleanup_new_scope(scope, parent)
                    else:
                        try:
                            resume_scope(parent, game, scope.name)
                            resumable = "AVAILABLE"
                        except WorkspaceError:
                            resumable = "UNPROVEN"
                        print(
                            f"DISCOVERY_SCOPE=RETAINED\nDISCOVERY_RUN_ID={scope.name}\nDISCOVERY_RESUME={resumable}",
                            flush=True,
                        )
                if failed:
                    break
    finally:
        OPTIONS.reset(token)
    if args.game in {None, "all"}:
        print("GAME=hsr\nSTATUS=SKIPPED\nERROR_CODE=GAME_PAUSED", flush=True)
    status = "FAIL" if failed else "PASS" if role_count else "NO_ROLES"
    print(f"DISCOVERY_STATUS={status}", flush=True)
    if not role_count and args.operation != "discover":
        print("ERROR_CODE=NO_OWNED_ROLES\nCOLLECTION_STATUS=NOT_COLLECTED", flush=True)
        return 1
    return 1 if failed else 0


def cached_roles(game: str, value: object) -> tuple[DiscoveredRole, ...]:
    # This is a saved selection, never evidence of authentication or ownership.
    module = importlib.import_module(f"hoyo_roster.games.{game}.login_provider")
    regions = module.SUPPORTED_REGIONS if game == "genshin" else {module.EXPECTED_REGION}
    if (
        not isinstance(value, dict)
        or set(value) != {"authority", "game", "roles"}
        or value["authority"] != "hoyo-roster/owned-role-selection-v1"
        or value["game"] != game
        or not isinstance(value["roles"], list)
    ):
        raise WorkspaceError("DISCOVERY_SELECTION_INVALID")
    result = []
    seen = set()
    for item in value["roles"]:
        if not isinstance(item, dict) or set(item) != {"uid", "region"}:
            raise WorkspaceError("DISCOVERY_SELECTION_INVALID")
        uid, region = item["uid"], item["region"]
        if (
            not isinstance(uid, str)
            or not uid.isascii()
            or not uid.isdigit()
            or uid.startswith("0")
            or len(uid) > 32
            or uid in seen
            or not isinstance(region, str)
            or region not in regions
        ):
            raise WorkspaceError("DISCOVERY_SELECTION_INVALID")
        seen.add(uid)
        result.append(DiscoveredRole(game, uid, region))
    return tuple(result)


async def run_selected_roles(roles, args) -> int:
    from .cli import run

    for role in roles:
        selected = argparse.Namespace(**vars(args))
        selected.game, selected.uid = role.game, role.uid
        selected.resume_discovery = None
        selected.discovered_region = role.region
        if await run(selected):
            return 1
    return 0
